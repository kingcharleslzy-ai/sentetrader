import { foreignStrategyScope, requestJson, type AiExecutionSnapshot, type InstrumentCatalog, type StrategyFunds, type StrategyMonitorSnapshot } from "./api";
import { normalizeWatchlist, watchlistMainSymbol } from "./components/futures/support";

import type { components } from "./workbench.generated";
import { useQuery } from "@tanstack/react-query";
import { readNetworkMode, readQueryClient, responseData, useReadIdentity, workbenchClient } from "./runtimeClient";

type Schemas = components["schemas"];
export type FeeRate = Schemas["FeeRate"];
export type FeeSchedule = Required<Schemas["FeeSchedule"]>;
export const defaultFees: FeeSchedule = { source: "account", default: { mode: "per_lot", open: 0, closeToday: 0, closeYesterday: 0,
  openPerLotExtra: 0, closeTodayPerLotExtra: 0, closeYesterdayPerLotExtra: 0 }, overrides: [] };
export type Operand = Schemas["Operand"];
export type RuleCondition = Schemas["Condition"];
export type RuleGroup = Schemas["RuleGroup"];
export type EntryTrigger = Schemas["EntryTrigger"];
export type Clarification = Schemas["Clarification"];
export type InstrumentScope = Required<Schemas["InstrumentScope"]>;
// Runtime returns normalized plans: default collections and rule slots exist. The building-block
// defaults (2026-09-25) stay optional: plans saved by or read from an older Runtime lack them.
/**
 * 按资金比例定手数与回撤停机 (2026-10-09): `equity_percent` sizes each open from the strategy's share of the equity
 * (`equityPercent`, with an optional total cap `maxLots`, no default); `maxDrawdownPercent` stops the strategy and
 * closes its own position once its own equity falls that far from its peak. Both are off unless the user sets them.
 */
export type SizingMode = NonNullable<Schemas["Sizing"]["mode"]>;
export type StrategyPlan = Omit<Schemas["StrategyPlan"], "rules" | "questions" | "closeBeforeEndScope" | "allowNightEntries" | "reminderOnly"> & {
  rules: Required<Schemas["StrategyRules"]>; questions: string[];
  closeBeforeEndScope?: Schemas["StrategyPlan"]["closeBeforeEndScope"]; allowNightEntries?: boolean;
  /** 提醒页的提醒 (第 6 批): only conditions and 时段, started as 只提醒; absent on older plans. */
  reminderOnly?: boolean;
};
export type SavedStrategy = { strategyId: string; name: string; symbol: string; version: number; status: string;
  /** 执行方式 of a running instance (2026-09-25); absent on an older Runtime (= auto). */
  signalAction?: "alert" | "confirm" | "auto";
  /** 说人话 (2026-09-25): 详情's one sentence (the template's plain words, or the Runtime's describe_plan). */
  planSummary?: string | null;
  /** 策略页改版第八批 (2026-10-08): 开仓 / 平仓 / 止损 / 风险 in plain words, a professional name as a
   *  part's `term` (shown behind “?”); and the newest stored backtest's summary for the card. */
  planRows?: PlanRow[] | null;
  latestBacktest?: LatestBacktest | null;
  definition: { schemaVersion: number; plan?: StrategyPlan; instrumentScope?: InstrumentScope; libraryStrategyId?: string;
    /** The chart period a follow-chart instance was bound to when it started. */
    chartTimeframeSeconds?: number;
    /** 策略模板库 (2026-09-25): the template and parameters a strategy was created from. */
    templateSource?: { templateId: string; templateName: string; params: Record<string, number | string | null>; defaults: boolean } } };
export interface PlanRow { key: "entry" | "exit" | "stop" | "risk"; label: string; parts: { text: string; term?: string; help?: string }[] }
export interface LatestBacktest { backtestId: string; netProfit: number; maxDrawdown: number; winRate: number | null; trades: number;
  createdAt?: string; symbol?: string; timeframeSeconds?: number;
  coverage?: { firstTradingDay: string | null; lastTradingDay: string | null; tradingDays: number } | null }
export const ruleNames = { entryLong: "开多", exitLong: "平多", entryShort: "开空", exitShort: "平空" };
/** 价差 (2026-09-29 第一期): A − B（或 A×2 − B、A ÷ B）—— the same words as the Runtime's
 *  spread_words / spread_legs_words (strategy_spread.py), so the detail page and the alerts agree. */
export type SpreadSpec = NonNullable<StrategyPlan["spread"]>;
export function spreadWords(spread: SpreadSpec): string {
  const [a, b] = spread.legs;
  if (spread.kind === "ratio") return `${a.symbol} ÷ ${b.symbol}`;
  if (a.weight === 1 && b.weight === 1) return `${a.symbol} − ${b.symbol}`;
  return `${a.symbol}×${number(a.weight)} − ${b.symbol}×${number(b.weight)}`;
}
/** One spread unit's legs: 做多价差（或平空价差）= 买 A 卖 B, 做空 = the other way. */
export function spreadLegsWords(spread: SpreadSpec, action: string, units = 1): string {
  const [a, b] = spread.legs;
  const buy = action === "open_long" || action === "close_short";
  return `${buy ? "买" : "卖"}${a.symbol} ${a.lots * units} 手、${buy ? "卖" : "买"}${b.symbol} ${b.lots * units} 手`;
}
export const directions = { long: "只做多", short: "只做空", both: "多空双向" };
export const operatorNames = { gt: "高于", gte: "不低于", lt: "低于", lte: "不高于", cross_up: "上穿", cross_down: "下穿",
  rising: "上升", falling: "下降", eq: "等于" };
const number = (value: number) => String(Number(value.toFixed(6)));
/** 枢轴位 levels (`value` of a pivot operand) and the R-Breaker names of TqSdk's demo. */
const pivotLevels: Record<string, string> = { "0": "枢轴P", "1": "枢轴R1（反转卖出价）", "2": "枢轴R2（观察卖出价）", "3": "枢轴R3（突破买入价）",
  "-1": "枢轴S1（反转买入价）", "-2": "枢轴S2（观察买入价）", "-3": "枢轴S3（突破卖出价）" };
const quadrants: Record<string, string> = { "1": "多头增仓", "2": "空头增仓", "3": "空头减仓", "4": "多头减仓" };
/** Same wording as the Runtime's describe_operand (strategy_rules.py), e.g. “今开 + 0.2×5日振幅”. */
export function operandLabel(value: Operand): string {
  const n = value.period ?? 5;
  const k = (fallback: number) => number(value.k ?? fallback);
  const labels: Record<Operand["indicator"], string> = {
    constant: number(value.value ?? 0), close: "收盘价", ma: `MA${n}`, ema: `EMA${n}`, macd_dif: "MACD DIF", macd_dea: "MACD DEA",
    macd_hist: "MACD 柱", kd_k: "KD K", kd_d: "KD D", volume: "成交量", volume_ratio: "放量倍数", atr: `ATR${n}`,
    donchian_high: `前${n}根最高`, donchian_low: `前${n}根最低`, boll_mid: `布林中轨(${n})`, boll_up: `布林上轨(${n},${k(2)})`,
    boll_down: `布林下轨(${n},${k(2)})`, keltner_mid: `肯特纳中轨(${n})`, keltner_up: `肯特纳上轨(${n},${k(1.5)})`,
    keltner_down: `肯特纳下轨(${n},${k(1.5)})`, rsi: `RSI${n}`, supertrend: `超级趋势(${n},${k(3)})`, prev_high: "昨高",
    prev_low: "昨低", prev_close: "昨收", today_open: "今开", range_nday: `${n}日振幅`, gap_percent: "开盘跳空%",
    opening_range_high: `开盘${n}根最高`, opening_range_low: `开盘${n}根最低`,
    cci: `CCI${n}`, adx: `ADX${n}`, plus_di: `+DI${n}`, minus_di: `−DI${n}`, sar: "抛物线SAR", session_high: "当日最高",
    session_low: "当日最低", pivot: pivotLevels[String(value.value ?? 0)] ?? "枢轴", oi_change: `持仓量较${n}根前变化`,
    oi_quadrant: "持仓四象限", roc: `${n}根涨跌幅%`, kd_j: "KDJ J", trix: `TRIX${n}`, high: "最高价", low: "最低价", open: "开盘价",
    williams_r: `威廉%R${n}`, bias: `乖离率${n}`, mfi: `MFI${n}`, bbi: "BBI", cmi: `震荡指数CMI${n}`, bar_range: "K线振幅",
    body: "K线实体", upper_shadow: "上影线", lower_shadow: "下影线", vwap: "分时均价",
  };
  let text = labels[value.indicator];
  if (value.average) text += `的${value.average}均线`;
  if (value.highest || value.lowest) text += `的${value.highest || value.lowest}根${value.highest ? "最高" : "最低"}`;
  const offset = value.offset;
  if (offset) text += ` ${offset.factor > 0 ? "+" : "−"} ${number(Math.abs(offset.factor))}×${offset.indicator === "atr" ? `ATR${offset.period ?? 14}` : `${offset.period ?? 5}日振幅`}`;
  if (value.barsAgo) text = `${value.barsAgo}根前的${text}`;
  return text;
}
/** Same wording as the Runtime's describe_condition, e.g. “收盘价 较2根前上升（连续 3 根）”. */
export function conditionText(condition: RuleCondition): string {
  const left = operandLabel(condition.left);
  const k = number(condition.right.value ?? 0);
  const text = condition.operator === "rising" || condition.operator === "falling"
    ? `${left} 较${k}根前${operatorNames[condition.operator]}`
    : condition.operator === "eq" ? `${left} = ${quadrants[k] ?? k}`
      : `${left} ${operatorNames[condition.operator]} ${operandLabel(condition.right)}`;
  const streak = (condition.consecutive ?? 1) > 1 ? `${text}（连续 ${condition.consecutive} 根）` : text;
  return (condition.minCount ?? 1) > 1 ? `${streak}（${condition.withinBars}根内至少${condition.minCount}根）` : streak;
}
/** 条件组合 (第四批): a group's own conditions and its branches in brackets, e.g. “（A 且 B） 或 （C 且 D）”. */
export function groupText(group: RuleGroup): string {
  const parts = [...(group.conditions ?? []).map(conditionText),
    ...(group.branches ?? []).map(branch => `（${branch.conditions.map(conditionText).join(branch.join === "all" ? " 且 " : " 或 ")}）`)];
  return parts.join(joinWord(group));
}
/**
 * One condition as the detail page's rule row: left operand, the verb, right operand, and the short
 * notes — the label's trailing （金叉）, 连续 N 根, 最近 N 根内. Same words as `conditionText`.
 */
export function conditionParts(condition: RuleCondition): { left: string; op: string; right: string; note: string } {
  const left = operandLabel(condition.left);
  const k = number(condition.right.value ?? 0);
  const [op, right] = condition.operator === "rising" || condition.operator === "falling"
    ? [`较${k}根前${operatorNames[condition.operator]}`, ""]
    : condition.operator === "eq" ? ["=", quadrants[k] ?? k]
      : [operatorNames[condition.operator], operandLabel(condition.right)];
  const notes = [condition.label.match(/[（(]([^（）()]{1,12})[）)]\s*$/)?.[1],
    (condition.consecutive ?? 1) > 1 ? `连续 ${condition.consecutive} 根` : undefined,
    (condition.minCount ?? 1) > 1 ? `最近 ${condition.withinBars} 根里至少 ${condition.minCount} 根`
      : condition.withinBars > 1 ? `最近 ${condition.withinBars} 根内` : undefined].filter(Boolean);
  return { left, op, right, note: notes.join("，") };
}
/** 盘中触价 (第五批): whether the price has to rise to the level — a buy stop / sell limit. */
export function triggerRises(trigger: EntryTrigger, long: boolean): boolean { return long === ((trigger.kind ?? "stop") === "stop"); }
/** Plan-level building blocks for the detail page: the exit rows (出场) or the sizing / entry-gate rows (仓位与预算). */
export function planBlockRows(plan: StrategyPlan, part: "exit" | "entry"): [string, string][] {
  const exits: [string, string][] = [];
  const entries: [string, string][] = [];
  const trailing = plan.trailingStop;
  if (trailing) {
    const unit = trailing.mode === "percent" ? "%" : trailing.mode === "ticks" ? " 跳" : `×ATR${trailing.atrPeriod ?? 14}`;
    exits.push(["追踪止损", `从持仓后的最高 / 最低价回撤 ${number(trailing.value)}${unit}，只收紧不放松${trailing.activateAfter ? `；浮盈达 ${number(trailing.activateAfter)}${unit} 后开始` : ""}`]);
  }
  const breakeven = plan.breakeven;
  if (breakeven) {
    const trigger = breakeven.triggerTicks != null ? `${breakeven.triggerTicks} 跳` : `${number(breakeven.triggerAtr ?? 0)}×ATR14`;
    exits.push(["保本", `浮盈达 ${trigger} 后止损移到${breakeven.offsetTicks ? `开仓价盈利一侧 ${breakeven.offsetTicks} 跳` : "开仓价"}，只收紧不放松`]);
  }
  if (plan.timeTakeProfit?.length) {
    const tiers = [...plan.timeTakeProfit].sort((a, b) => a.afterBars - b.afterBars).map(tier =>
      `持仓满 ${tier.afterBars} 根起浮盈 ≥ ${tier.profitTicks != null ? `${tier.profitTicks} 跳` : `${number(tier.profitAtr ?? 0)}×ATR14`}`);
    exits.push(["分段止盈", `K线收盘时判断：${tiers.join("；")}，达到就平仓`]);
  }
  if (plan.scaleOut?.length) {
    const tiers = plan.scaleOut.map(tier =>
      `盈利 ${tier.profitTicks != null ? `${tier.profitTicks} 跳` : `${number(tier.profitAtr ?? 0)} 倍 ATR(14)`}平 ${tier.percent}%`);
    exits.push(["分批止盈", `${tiers.join("，")}；按持仓均价判断，每档一次，剩下的照原止损止盈走`]);
  }
  if (plan.closeBeforeEndMinutes) exits.push(["收盘前平仓", `${plan.closeBeforeEndScope === "session" ? "每个交易时段" : "当日最后一个时段"}收盘前 ${plan.closeBeforeEndMinutes} 分钟平仓`]);
  if (plan.maxHoldBars) exits.push(["最长持仓", `持仓满 ${plan.maxHoldBars} 根K线平仓`]);
  if (plan.sizing?.mode === "atr_risk") entries.push(["按 ATR 定手数", `权益 × ${number(plan.sizing.riskPercent ?? 0)}% ÷ (合约乘数 × ${number(plan.sizing.atrMultiple ?? 2)}×ATR${plan.sizing.atrPeriod ?? 14})，最多 ${plan.maxPosition} 手`]);
  if (plan.sizing?.mode === "equity_percent") entries.push(["按资金比例定手数", `本策略持仓和挂单的保证金最多占权益 ${number(plan.sizing.equityPercent ?? 0)}%，每次按剩余额度 ÷ 每手保证金算手数`
    + `${plan.sizing.maxLots ? `，总共最多 ${plan.sizing.maxLots} 手` : ""}；不足 1 手这次不开`]);
  if (plan.maxDrawdownPercent != null) entries.push(["回撤停机", `本策略权益从最高点回撤 ${number(plan.maxDrawdownPercent)}% 时停新开仓、撤本策略挂单、平本策略持仓，之后保持停止，要手动恢复`]);
  if (plan.pyramiding) {
    const pyramid = plan.pyramiding;
    const trigger = pyramid.trigger === "profit_ticks" ? `每盈利 ${number(pyramid.step)} 跳`
      : pyramid.trigger === "signal" ? "同向开仓信号再成立且收盘更优时" : `每盈利 ${number(pyramid.step)} 倍 ATR(${pyramid.atrPeriod ?? 20})`;
    const reserve = Math.max(1, plan.maxPosition - (pyramid.volume || 1) * pyramid.maxAdds);
    entries.push(["加仓", `${trigger}加 ${pyramid.volume ? `${pyramid.volume} 手` : "首次开仓的手数"}，最多加 ${pyramid.maxAdds} 次`
      + `${pyramid.moveStop === false ? "" : "，止损随最近一次加仓收紧"}；首笔开 ${number(reserve)} 手给加仓留空间`
      + `（最大持仓 ${number(plan.maxPosition)} − 每次加仓手数 × 最多加仓次数，最少 1 手）`]);
  }
  if (plan.entryWindows?.length) entries.push(["开仓时段", plan.entryWindows.map(window => `${window.start}–${window.end}`).join("、")]);
  if (plan.allowNightEntries === false) entries.push(["夜盘", "夜盘不开新仓，出场照常"]);
  if (plan.maxEntriesPerDirection) entries.push(["每方向开仓", `每个交易日每个方向最多 ${plan.maxEntriesPerDirection} 次`]);
  for (const [key, side] of [["entryLong", "开多"], ["entryShort", "开空"]] as const) {
    const trigger = plan.rules[key]?.trigger;
    if (trigger) entries.push(["盘中触价", `${side}条件收盘成立后挂价「${operandLabel(trigger.price)}」，下一根K线里价格${triggerRises(trigger, key === "entryLong") ? "涨到" : "跌到"}它就${side}，没触及就作废`]);
    const rearm = plan.rules[key]?.rearm;
    if (rearm?.length) entries.push(["复位", `${side}后须等「${rearm.map(conditionText).join("且")}」成立一次才再${side}`]);
  }
  return part === "exit" ? exits : entries;
}
/**
 * 填表单 (说人话, 2026-09-25): which blocks are 常用 (direction, period, 均线 / 突破 / RSI-style conditions,
 * trailing stop, close-before-end) and which are 高级 — 复位, 连续 N 根, 升降, 持仓量, 枢轴, CCI / ADX /
 * SAR / 当日最高最低, 方向确认, 分段止盈, 保本, 时段, 每方向次数, 最长持仓, 按 ATR 定手数, 回调结构;
 * the third batch (2026-09-27) adds ROC / KDJ J / TRIX, the bar's high / low / open and 前 N 根 (barsAgo);
 * the fourth adds K线形态 (振幅 / 实体 / 影线), CMI, 区间最高 / 最低, N 根里至少 M 根 and 条件组合 (a
 * group with branches goes to 高级 whole). 威廉 %R / 乖离率 / MFI / BBI are 常用, like RSI.
 * 第六批 (2026-09-28): 加仓 and 分批止盈 rows — 高级, like the other plan-level exits.
 * Every condition and plan block lands in exactly one list; the designer folds the 高级 one and opens
 * it whenever it is not empty. (Stop / take-profit, lots and the loss cap are the designer's own 常用 rows.)
 */
const ADVANCED_INDICATORS = new Set(["oi_change", "oi_quadrant", "pivot", "cci", "adx", "plus_di", "minus_di", "sar", "session_high", "session_low",
  "roc", "kd_j", "trix", "high", "low", "open", "cmi", "bar_range", "body", "upper_shadow", "lower_shadow", "vwap"]);
const COMMON_PLAN_ROWS = new Set(["追踪止损", "收盘前平仓"]);
export function isAdvancedCondition(condition: RuleCondition): boolean {
  return (condition.consecutive ?? 1) > 1 || ["rising", "falling", "eq"].includes(condition.operator)
    || ADVANCED_INDICATORS.has(condition.left.indicator) || ADVANCED_INDICATORS.has(condition.right.indicator)
    || Boolean(condition.left.barsAgo || condition.right.barsAgo) || (condition.minCount ?? 1) > 1
    || [condition.left, condition.right].some(side => Boolean(side.highest || side.lowest));
}
const joinWord = (group: RuleGroup) => group.sequence ? "，再 " : group.join === "all" ? " 且 " : " 或 ";
export function designerRows(plan: StrategyPlan): { common: [string, string][]; advanced: [string, string][] } {
  const common: [string, string][] = [["方向", directions[plan.direction]], ["周期", periodLabel(plan.timeframeSeconds)]];
  const advanced: [string, string][] = [];
  for (const key of Object.keys(ruleNames) as (keyof typeof ruleNames)[]) {
    const group = plan.rules[key];
    if (!group) continue;
    // A group with 条件组合 reads as one bracketed expression: it moves to 高级 whole.
    if (group.branches?.length) { advanced.push([`${ruleNames[key]} · 条件组合`, groupText(group)]); continue; }
    // A sequence is one ordered chain of events: it moves to 高级 as a whole when any link is 高级.
    const conditions = group.conditions ?? [];
    const plain = group.sequence ? conditions.some(isAdvancedCondition) ? [] : conditions : conditions.filter(item => !isAdvancedCondition(item));
    const extra = conditions.filter(item => !plain.includes(item));
    if (plain.length) common.push([ruleNames[key], plain.map(conditionText).join(joinWord(group))]);
    if (group.alignmentConditions?.length) advanced.push([`${ruleNames[key]} · 方向确认`, group.alignmentConditions.map(conditionText).join(" 且 ")]);
    if (extra.length) advanced.push([plain.length ? `${ruleNames[key]} · ${group.join === "all" ? "还须满足" : "或者"}` : ruleNames[key],
      extra.map(conditionText).join(joinWord(group))]);
  }
  if (plan.pullback) advanced.push(["回调结构", `MA${plan.pullback.trendPeriod} 顺势回调到 MA${plan.pullback.touchPeriod}，守住 MA${plan.pullback.guardPeriod}，最多等 ${plan.pullback.maxBars} 根`]);
  for (const [label, text] of [...planBlockRows(plan, "exit"), ...planBlockRows(plan, "entry")])
    (COMMON_PLAN_ROWS.has(label) ? common : advanced).push([label, text]);
  return { common, advanced };
}
/**
 * The designer's editable 加仓 / 分批止盈 (第六批, 2026-09-28): the same checks the server's
 * StrategyPlan validation runs, so an invalid edit is refused in the form with the same Chinese
 * line instead of a 422 on save. Plans without these two blocks are always fine.
 */
export function designerPlanProblem(plan: StrategyPlan): string | null {
  // 策略编辑卡第二版 (2026-10-08): the blocks the card now edits, with the server's own limits and wording.
  if (!(plan.maxPosition >= 1 && plan.maxPosition <= 20)) return "最多持有须在 1–20 手（加仓也算在内）";
  const breakeven = plan.breakeven;
  if (breakeven) {
    if ((breakeven.triggerTicks == null) === (breakeven.triggerAtr == null)) return "保本触发写跳数或 ATR 倍数其中一个";
    if (breakeven.triggerTicks != null && (breakeven.offsetTicks ?? 0) >= breakeven.triggerTicks) return "保本后的止损须在触发价以内（偏移跳数小于触发跳数）";
  }
  const targets = plan.timeTakeProfit ?? [];
  if (new Set(targets.map(tier => tier.afterBars)).size !== targets.length) return "按时间止盈各档的持仓根数不能重复";
  if (plan.sizing?.mode === "atr_risk" && !(plan.sizing.riskPercent != null && plan.sizing.riskPercent > 0 && plan.sizing.riskPercent <= 10))
    return "按 ATR 定手数须设置单笔风险占权益的百分比（大于 0、最多 10%）";
  if (plan.sizing?.mode === "equity_percent") {
    const { equityPercent, maxLots } = plan.sizing;
    if (!(equityPercent != null && equityPercent > 0 && equityPercent <= 100)) return "按资金比例定手数须设置资金占用上限（大于 0、最多 100%）";
    if (maxLots != null && !(Number.isInteger(maxLots) && maxLots >= 1)) return "按资金比例的最多手数须是正整数（不填为不限）";
  }
  if (plan.maxDrawdownPercent != null && !(plan.maxDrawdownPercent > 0 && plan.maxDrawdownPercent <= 100)) return "回撤停机的比例须大于 0、最多 100%";
  if (plan.cooldownBars != null && !(plan.cooldownBars >= 0 && plan.cooldownBars <= 100)) return "平仓后冷却须在 0–100 根";
  if (plan.maxEntriesPerDirection != null && !(plan.maxEntriesPerDirection >= 1 && plan.maxEntriesPerDirection <= 50)) return "每个方向每天最多开仓次数须在 1–50";
  if (plan.maxLoss != null && !(plan.maxLoss > 0)) return "当日亏损上限须大于 0";
  if (plan.perTradeRisk != null && !(plan.perTradeRisk > 0)) return "单笔预算须大于 0";
  if (plan.pullback && plan.perTradeRisk == null) return "回调结构止损需要单笔预算";
  if (plan.trailingStop && !(plan.trailingStop.atrPeriod == null || (plan.trailingStop.atrPeriod >= 2 && plan.trailingStop.atrPeriod <= 200))) return "追踪止损的 ATR 周期须在 2–200";
  if (plan.spread) {
    for (const [name, present] of [["追踪止损", plan.trailingStop], ["保本", plan.breakeven], ["分段止盈", plan.timeTakeProfit?.length], ["加仓", plan.pyramiding],
      ["分批止盈", plan.scaleOut?.length], ["收盘前平仓", plan.closeBeforeEndMinutes], ["单笔预算", plan.perTradeRisk != null]] as const) if (present) return `价差策略不使用${name}`;
    if (plan.sizing?.mode === "atr_risk") return "价差策略不按 ATR 定手数";
  }
  const pyramid = plan.pyramiding;
  if (pyramid) {
    if (plan.breakeven || plan.trailingStop || plan.timeTakeProfit?.length) return "加仓暂不和保本/追踪止损/分段止盈一起用";
    if (plan.maxPosition < 2) return "加仓须最大持仓至少 2 手";
    if (!(pyramid.maxAdds >= 1 && pyramid.maxAdds <= 5)) return "加仓最多加仓次数须在 1–5 次";
    if (!(pyramid.step > 0 && pyramid.step <= 10000)) return "加仓步长须大于 0";
    if (pyramid.trigger === "profit_atr" && !(pyramid.atrPeriod >= 2 && pyramid.atrPeriod <= 200)) return "加仓 ATR 周期须在 2–200";
    if (pyramid.volume != null && !(pyramid.volume >= 1 && pyramid.volume <= 20)) return "加仓每次手数须在 1–20 手（留空跟首笔）";
  }
  const tiers = plan.scaleOut ?? [];
  if (tiers.length > 3) return "分批止盈最多 3 档";
  for (const [index, tier] of tiers.entries()) {
    const hasTicks = tier.profitTicks != null, hasAtr = tier.profitAtr != null;
    if (hasTicks === hasAtr) return `分批止盈第 ${index + 1} 档须写跳数或 ATR 倍数其中一个`;
    if (hasTicks && !(tier.profitTicks! >= 1 && tier.profitTicks! <= 10000)) return `分批止盈第 ${index + 1} 档目标须在 1–10000 跳`;
    if (hasAtr && !(tier.profitAtr! > 0 && tier.profitAtr! <= 50)) return `分批止盈第 ${index + 1} 档目标须大于 0、最多 50 倍 ATR`;
    if (!(tier.percent >= 1 && tier.percent <= 99)) return `分批止盈第 ${index + 1} 档平掉百分比须在 1–99%`;
  }
  if (tiers.length) {
    if (tiers.some(tier => tier.profitTicks != null) && tiers.some(tier => tier.profitAtr != null))
      return "分批止盈各档须同用跳数或同用 ATR 倍数";
    const values = tiers.map(tier => tier.profitTicks ?? tier.profitAtr!);
    if (values.some((value, index) => index > 0 && value <= values[index - 1])) return "分批止盈各档目标须由近到远递增";
  }
  return null;
}
export function periodLabel(seconds: number): string { return seconds === 0 ? "跟随当前图表" : seconds === 86400 ? "日线" : `${seconds / 60} 分钟`; }
export function followChart(plan: StrategyPlan): StrategyPlan {
  const next = structuredClone(plan);
  next.timeframeSeconds = 0;
  for (const group of Object.values(next.rules)) for (const condition of [...(group?.alignmentConditions || []), ...(group?.conditions || []),
    ...((group && "rearm" in group ? group.rearm : undefined) || []), ...(group?.branches || []).flatMap(branch => branch.conditions)]) {
    condition.left.timeframe = 0;
    condition.right.timeframe = 0;
    condition.label = condition.label.replace(/(\d+)\s*日\s*均线/g, "MA$1");
  }
  for (const group of [next.rules.entryLong, next.rules.entryShort]) if (group?.trigger) group.trigger.price.timeframe = 0;
  next.description = next.description.replace(/(\d+)\s*日\s*均线/g, "MA$1");
  next.description += "\n周期更正：以上指标统一跟随当前图表周期，均线数字表示 K 线根数。";
  return next;
}
export async function setChartPeriod(url: string, symbol: string, timeframeSeconds: number) {
  const allowed = [60, 180, 300, 900, 3600, 86400];
  if (!allowed.includes(timeframeSeconds)) throw new Error("请选择有效图表周期");
  return responseData(await workbenchClient(url).POST("/api/strategy-plans/chart-period", {
    body: { symbol, timeframeSeconds: timeframeSeconds as Schemas["ChartPeriodRequest"]["timeframeSeconds"] }
  }));
}
/**
 * The strategy list (`/api/strategy-plans`): one cache entry per Runtime that every panel reads. A write calls
 * strategiesChanged() and every panel showing the list reads it again; each panel keeps its own refresh cadence.
 */
export type StrategyList = { items: SavedStrategy[]; executions?: SavedStrategy[] };
export function useStrategies(url: string | undefined, refetchInterval: number | (() => number) | false = false) {
  const identity = useReadIdentity();
  return useQuery({ queryKey: ["strategies", identity, url], enabled: url !== undefined, networkMode: readNetworkMode(url),
    queryFn: () => loadStrategies(url!), refetchInterval }, readQueryClient);
}
/** A one-off read that must see the latest list (a strategy made a moment ago): its own request, never one already
 *  under way that may have left before that strategy existed. Panels pick the strategy up on their next read. */
export function loadStrategies(url: string) { return requestJson<StrategyList>(`${url}/api/strategy-plans`); }
/** After a strategy write: every panel showing the list reads it again (resolves once they have). */
export function strategiesChanged() { return readQueryClient.invalidateQueries({ queryKey: ["strategies"] }); }
export function strategyScope(item: SavedStrategy): InstrumentScope { return item.definition.instrumentScope || {mode: "selected", symbols: [item.symbol]}; }
export function scopeLabel(item: SavedStrategy): string { const scope = strategyScope(item); return scope.mode === "any" ? "全部品种" : scope.symbols.join("、"); }
/** 能上线的具体合约（与 Runtime 的 STRATEGY_CONTRACT_PATTERN 同一写法）：国内 SHFE.rb2701、外盘月份 CME.GCEZ26，
 *  以及外盘图表的快期外盘主连 KQD.m@COMEX.GC——上线时 Runtime 换成美元子账户此刻的主力月份（外盘三期）。 */
export const CONTRACT = /^(?:[A-Za-z]+\.[A-Za-z][A-Za-z0-9]*(?:\d{3,4}|[FGHJKMNQUVXZ]\d{2})|KQD\.[A-Za-z]+@[A-Za-z]+\.[A-Za-z0-9]+)$/;
/** 具体合约 → 品种范围写法：SHFE.rb2701 → SHFE.rb，CME.GCEZ26 → CME.GCE。 */
const productOf = (symbol: string) => /^CME\.[A-Za-z0-9]+[FGHJKMNQUVXZ]\d{2}$/i.test(symbol) ? symbol.slice(0, -3) : symbol.replace(/\d+$/, "");
export function allowsSymbol(item: SavedStrategy, symbol: string): boolean {
  const scope = strategyScope(item); const normalized = symbol.toLowerCase();
  if (scope.mode === "any") return true;
  const product = (normalized.startsWith("kqd.") ? foreignStrategyScope(symbol) ?? "" : productOf(symbol)).toLowerCase();
  return scope.symbols.some(s => normalized === s.toLowerCase() || (!CONTRACT.test(s) && product === s.toLowerCase()));
}
/** The contract a library strategy is edited and backtested on (the desktop panel and the phone's 我的策略):
 *  the chart's when its scope allows it, else its scope's first concrete contract. */
export function strategySymbol(item: SavedStrategy, chartSymbol: string): string {
  return allowsSymbol(item, chartSymbol) ? chartSymbol : strategyScope(item).symbols.find(value => CONTRACT.test(value)) ?? chartSymbol;
}
/** The plan still has the designer's open questions: it is answered (完善策略) before a backtest or 上线. */
export const needsAnswersOf = (item: SavedStrategy | undefined) => Boolean(item?.definition.plan && (item.definition.plan.questions.length || item.definition.plan.clarifications?.length));
/** 上线运行's first check (the desktop dialog and the phone's 回测结果页): AI 模拟执行 stopped or
 *  emergency-stopped refuses with its line; HALT_OPENS still allows monitoring (strategies keep
 *  watching, only new opens are refused). Null = the launch may go on. */
export async function launchBlockedReason(url: string): Promise<string | null> {
  const control = await requestJson<AiExecutionSnapshot>(`${url}/api/ai-execution`);
  if (!control.controls) return "无法确认 AI 模拟执行状态，请检查连接后重试。";
  if (control.controls.stopStatus === "STOPPED" || control.controls.stopStatus === "EMERGENCY_STOP") return strategyStatusText("ai_execution_stopped");
  return null;
}
/**
 * 按资金比例 on the editor (2026-10-09): the Runtime's read-only estimate for this percentage on a contract — the same
 * calculation the monitor's `funds` and the send-time check use — for the account the request header names. With
 * `strategyId` it takes off what that strategy already holds and has pending; without, a strategy holding nothing.
 */
export async function previewStrategyFunds(url: string, request: { symbol: string; equityPercent: number; maxLots?: number | null;
  strategyId?: string | null }): Promise<StrategyFunds> {
  const query = new URLSearchParams({ symbol: request.symbol, equityPercent: String(request.equityPercent) });
  if (request.maxLots != null) query.set("maxLots", String(request.maxLots));
  if (request.strategyId) query.set("strategyId", request.strategyId);
  return (await requestJson<{ funds: StrategyFunds }>(`${url}/api/strategies/funds-preview?${query}`)).funds;
}

/** 恢复运行 after 回撤停机: the instance's own start with `resetDrawdown` — a new 起始 and peak; a plain start never resets. */
export async function resumeAfterDrawdown(url: string, strategyId: string) {
  return requestJson<StrategyMonitorSnapshot>(`${url}/api/strategies/${encodeURIComponent(strategyId)}/monitor/start`,
    { method: "POST", body: JSON.stringify({ resetDrawdown: true }) });
}

/** 验证并上线: validate on `symbol`, then start monitoring. `start` carries the 执行方式 and the
 *  conversation for reminders and cards; omitted, a paused instance keeps its own (继续). */
/**
 * 开始提醒's contracts (改版第一步 2026-10-31): the card's contract, or for 整张自选表 the main contract of every
 * watchlist product — one 只提醒 instance each, so every product is judged on its own.
 * shortcut: the list is read at the start; a later change to the watchlist is not followed until the next 开始提醒.
 */
export async function reminderContracts(url: string, item: SavedStrategy, contract: string, instruments?: InstrumentCatalog): Promise<string[]> {
  if (!item.definition.plan?.reminder?.watchlist) return [contract];
  const document = await requestJson<{ preferences: { watchlist?: { products?: unknown } } }>(`${url}/api/preferences`);
  const items = instruments?.items ?? [];
  const contracts = normalizeWatchlist(document.preferences.watchlist?.products).map(product => watchlistMainSymbol(items, product))
    .filter((symbol): symbol is string => Boolean(symbol));
  if (!contracts.length) throw new Error("自选表里还没有品种（或合约目录还没读到）：先在报价里给品种点星，再开始提醒");
  return [...new Set(contracts)];
}

export async function validateAndStart(url: string, id: string, symbol: string, chartTimeframeSeconds: number, onValidated?: (executionId: string) => void,
  start: { signalAction?: "alert" | "confirm" | "auto"; sessionId?: string } = {}) {
  const validation = await requestJson<{strategyId: string}>(`${url}/api/strategy-plans/${id}/validate`, { method: "POST", body: JSON.stringify({symbol, chartTimeframeSeconds}) });
  onValidated?.(validation.strategyId);
  await requestJson(`${url}/api/strategies/${validation.strategyId}/monitor/start`, { method: "POST", body: JSON.stringify(start) });
  return validation.strategyId;
}


const userTerms: Record<string, string> = {
  withinBars: "条件有效根数", maxPosition: "最大持仓手数", takeProfitTicks: "止盈跳数",
  stopLossTicks: "止损跳数", maxOrdersPerDay: "每天最多开仓次数", cooldownBars: "平仓后等待根数",
  timeframeSeconds: "执行周期秒数", maxLoss: "当日亏损上限", entryLong: "开多", exitLong: "平多",
  entryShort: "开空", exitShort: "平空",
  alignmentConditions: "方向确认条件", stopLookbackBars: "止损参考K线根数",
};
export function strategyText(text: string): string {
  return text.replace(/\b(withinBars|maxPosition|takeProfitTicks|stopLossTicks|maxOrdersPerDay|cooldownBars|timeframeSeconds|maxLoss|entryLong|exitLong|entryShort|exitShort|alignmentConditions|stopLookbackBars)\b/g, name => userTerms[name]);
}
const strategyStatusTerms: Record<string, string> = {
  ai_execution_stopped: "AI 模拟执行已停止或急停；请点击顶部的恢复入口，确认恢复模拟执行后再启动策略",
  ai_new_opens_halted: "已只停新开仓，策略只监控不开仓；恢复模拟执行后继续",
  market_session_closed: "当前不在交易时段，等待开盘",
  strategy_drawdown_liquidating: "回撤停机后本策略的挂单和持仓还在处理，处理完才能恢复运行",
  strategy_drawdown_locked: "回撤停机已触发，本策略暂停开新仓，恢复运行后才会再开",
  strategy_funds_budget_exceeded: "超出本策略按资金比例的额度，这次不开",
  funds_preview_unavailable: "行情或账户数据暂不可用，暂时算不出额度",
  strategy_connection_initializing: "账户与行情正在连接，请稍候",
  strategy_market_not_fresh: "所需周期行情正在连接或已过期，请刷新行情后重试",
  strategy_bars_insufficient: "所需周期的已收盘 K 线不足，请等待行情载入后重试",
  strategy_monitor_unavailable: "模拟监控服务尚未就绪，请检查账户与行情连接",
  strategy_validation_not_passed: "规则验证尚未通过，请重新验证当前版本",
  strategy_transition_invalid: "策略状态已变化，请刷新列表后重试",
  not_execution_owner: "这个账户的 AI 自主执行由执行负责人管理，你只能生成方案卡",
  ai_autonomy_disabled: "AI 自主执行已关闭，不能选自动下单；在设置 → 交易里打开后再选",
  account_read_only: "你对这个账户只有查看权限",
  account_not_granted: "你没有这个账户的访问授权",
};
// Runtime status/attempt text is a separate boundary from user-authored names and rules.
export function strategyStatusText(text: string, fallback = "状态详情暂不可用，请查看运行记录或使用 AI 诊断"): string {
  const translated = strategyText(Object.entries(strategyStatusTerms).reduce((value, [code, label]) => value.replaceAll(code, label), text));
  if (/\b[a-zA-Z][a-zA-Z0-9]*_[a-zA-Z0-9_]+\b|\b\w*(?:Error|Exception)\b/.test(translated) || !/[\u3400-\u9fff]/.test(translated)) return fallback;
  return translated;
}
export function strategyQuestions(plan: StrategyPlan): Clarification[] {
  if (plan.clarifications?.length) return plan.clarifications;
  return plan.questions.slice(0, 3).map((question, index) => ({
    id: `legacy-${index}`, question, recommended: -1,
    options: [],
  }));
}
export function requestStatus(error: unknown): number | undefined {
  return (error as {status?: number})?.status || Number((error instanceof Error ? error.message : "").match(/^(\d{3}) /)?.[1]) || undefined;
}
export function strategyError(error: unknown, fallback: string, closedMessage?: string): string {
  const status = requestStatus(error);
  if (status === 403) {
    // 4.3 执行归属与角色拒绝：detail 是裸错误码（not_execution_owner / account_read_only /
    // account_not_granted），不是连接失效。
    const detail = (error as {detail?: unknown})?.detail;
    const code = typeof detail === "string" ? detail : (detail as {code?: string} | undefined)?.code;
    if (code && strategyStatusTerms[code]) return strategyStatusTerms[code];
  }
  if (status === 401 || status === 403) return "连接已失效，请在设置中重新连接后重试。";
  let body = (error as {detail?: unknown})?.detail;
  if (!body && error instanceof Error) { try { body = JSON.parse(error.message.replace(/^\d{3} /, "")); } catch { /* Network error has no response body. */ } }
  const detail = (body as {detail?: unknown})?.detail || body;
  // A structured refusal ({code}) reads through the same terms as a plain one.
  const message = typeof detail === "string" ? detail : (detail as {message?: string})?.message ?? (detail as {code?: string})?.code;
  if (message) {
    if (closedMessage && message.includes("strategy_market_not_fresh")) return closedMessage;
    return strategyStatusText(message, fallback);
  }
  return status ? fallback : "无法连接应用服务，请检查连接后重试。";
}

import type { StrategyBacktestResult } from "./strategyBacktests";
import type { StrategyPlan } from "./strategyApi";
import { numberProblem } from "./numberField";
import { DEFAULTS, DIRECTIONS, LOTS_RULE, MAX_ORDERS_RULE, SIDE_WORDS, TAKE_TICKS_RULE, effectiveDirection, hitsOf, maxPositionOf, missingParts, rowComplete, rowContext, rowLabel, stopText, timeframeLabel, usedEntryGroups,
  type Diagnosis, type EditorForm, type EditorSection, type GroupKey } from "./strategyEditor";

/**
 * 策略流程图 (策略编辑卡第二版, 2026-10-08; wide.html): the form drawn as the order the engine runs it — 触发 (each closed
 * K-line) → 过滤 (时段 / 方向 / 夜盘) → 开多 / 开空 condition groups → 开仓动作 → each way out → 冷却 / 每天次数 / 平均持仓.
 * Every count comes from the card's last preview backtest (exit reasons, the per-condition diagnosis, the average
 * holding) and only while that result is the form's own (`fresh`); a count it does not carry is left out, never made
 * up. A block the form still misses (开仓条件, 止损) is `pending` (虚线、待填). The same model is the snapshot's `flow`.
 */
export type FlowTarget = "trigger" | "entryLong" | "entryShort" | "action" | "stop" | "take" | "trailing" | "breakeven" | "scaleOut"
  | "timeTakeProfit" | "maxHold" | "closeBeforeEnd" | "exitRules" | "limits";
/** `strategyEditor {focus}`: a section, or a flow block (the field it stands for). */
export type FocusTarget = EditorSection | FlowTarget;
export const FOCUS_TARGETS: FocusTarget[] = ["basics", "entry", "exit", "sizing", "trigger", "entryLong", "entryShort", "action", "stop", "take",
  "trailing", "breakeven", "scaleOut", "timeTakeProfit", "maxHold", "closeBeforeEnd", "exitRules", "limits"];
/** The section a focus target lives in, and whether it sits in that section's 更多 fold. */
export function focusPlace(target: FocusTarget): { section: EditorSection; more: boolean } {
  if (target === "basics" || target === "trigger") return { section: "basics", more: false };
  if (target === "entry" || target === "entryLong" || target === "entryShort") return { section: "entry", more: false };
  if (target === "sizing" || target === "action") return { section: "sizing", more: false };
  if (target === "limits") return { section: "sizing", more: false };
  return { section: "exit", more: ["breakeven", "scaleOut", "timeTakeProfit", "maxHold", "exitRules"].includes(target) };
}

export interface FlowRow { text: string; count: number | null }
export interface FlowBox {
  id: FlowTarget; title: string; lines: string[];
  /** 回测里实际发生的次数 (满足 / 触发); null = the preview does not say. */
  count: number | null;
  /** The net of the trades that left this way (for the count's colour); null when unknown. */
  net?: number | null;
  rows?: FlowRow[];
  side?: "long" | "short";
  pending?: boolean;
}
export interface StrategyFlowModel {
  /** “近 60 个交易日” while the counts are the current form's preview, null otherwise. */
  basis: string | null;
  trigger: FlowBox; entries: FlowBox[]; action: FlowBox; exits: FlowBox[]; notes: string[];
}

type Preview = StrategyBacktestResult & { diagnosis?: Partial<Record<"entryLong" | "entryShort", Diagnosis>> };
const EXIT_REASONS: Partial<Record<FlowTarget, string[]>> = { stop: ["stop_loss", "spread_stop"], take: ["take_profit", "spread_take"], trailing: ["trailing_stop"],
  breakeven: ["breakeven"], scaleOut: ["scale_out"], timeTakeProfit: ["time_take_profit"], maxHold: ["max_hold"], closeBeforeEnd: ["close_before_end"], exitRules: ["rule"] };
const CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫";

/**
 * 平均持仓 as a duration on the strategy's own period: bars × period is trading time (minutes / hours); once that is
 * a trading day or more it is counted in the backtest's own bars per trading day (coverage.bars ÷ tradingDays), so
 * night sessions and lunch breaks are not counted as hours. The bar count goes in brackets.
 */
export function holdingText(bars: number, timeframe: number, coverage?: { bars?: number; tradingDays?: number }): string {
  const count = `${Math.round(bars)} 根`;
  const decimal = (value: number) => value >= 10 ? String(Math.round(value)) : String(Number(value.toFixed(1)));
  if (timeframe >= 86400) return `平均持仓约 ${decimal(bars)} 个交易日`;
  const perDay = coverage?.bars && coverage.tradingDays ? coverage.bars / coverage.tradingDays : null;
  if (perDay && bars >= perDay) return `平均持仓约 ${decimal(bars / perDay)} 个交易日（${count}）`;
  const minutes = bars * timeframe / 60;
  return minutes >= 60 ? `平均持仓约 ${decimal(minutes / 60)} 小时（${count}）` : `平均持仓约 ${Math.max(1, Math.round(minutes))} 分钟（${count}）`;
}

export function strategyFlow(form: EditorForm, plan: StrategyPlan, result: Preview | undefined, fresh: boolean, chartTimeframe = 300): StrategyFlowModel {
  const counted = fresh && result ? result : undefined;
  const timeframe = form.timeframe || chartTimeframe;
  const direction = effectiveDirection(form);
  const missing = missingParts(form);
  const filters = [form.entryWindows.filter(window => window.start && window.end).map(window => `${window.start}–${window.end}`).join("、"),
    DIRECTIONS.find(item => item.value === direction)?.label ?? "", form.allowNightEntries ? "" : "夜盘不开新仓"].filter(Boolean);
  const trigger: FlowBox = { id: "trigger", title: `每根 ${form.timeframe ? timeframeLabel(form.timeframe) : `${timeframeLabel(chartTimeframe)}（跟随图表）`} K 线收盘`,
    lines: [filters.join(" · ")].filter(Boolean), count: null };

  const entries: FlowBox[] = usedEntryGroups(form).filter(key => form[key].locked || form[key].rows.length).map(key => {
    const group = form[key];
    const { hits, together } = counted ? hitsOf(form, key, counted) : { hits: [], together: null };
    const side = key === "entryLong" ? "long" : "short";
    const rows: FlowRow[] = group.rows.map((row, index) => ({ text: `${CIRCLED[index] ?? `${index + 1}.`} ${rowComplete(row, { planTimeframe: form.timeframe }) ? rowLabel(row, form.timeframe) : "没填完"}`,
      count: hits[index] ?? null }));
    const title = group.locked ? `${key === "entryLong" ? "开多" : "开空"} · 条件组合（只读）`
      : `${key === "entryLong" ? "开多" : "开空"} · ${group.rows.length > 1 ? `${group.rows.length} 条${group.join === "any" ? "任一" : "全部"}满足` : "1 条"}`;
    // Only filled rows count: a blank 方向确认 / 复位 row is not a confirmation yet.
    const filled = (list: "alignment" | "rearm") => group[list].filter(row => rowComplete(row, rowContext(form, key, list))).length;
    const alignment = filled("alignment"), rearm = filled("rearm");
    const lines = [alignment ? `先等方向确认 ${alignment} 条全部成立` : "", rearm ? `开仓后复位 ${rearm} 条成立一次才再开` : ""].filter(Boolean);
    return { id: key as "entryLong" | "entryShort", title, lines, rows, count: together, side };
  });
  if (missing.includes("开仓条件")) entries.splice(0, entries.length, { id: usedEntryGroups(form)[0], title: "开仓条件", lines: ["待填"], count: null, pending: true });

  const lots = form.lots ?? DEFAULTS.lots;
  const most = maxPositionOf(form);
  const verb = direction === "long" ? "开多" : direction === "short" ? "开空" : "开多 / 开空";
  const sizing = form.sizing;
  // A count the card refuses (#755) is never drawn: the block says it waits for a fix.
  const lotsBad = sizing.mode !== "equity_percent" && numberProblem(form.lots, LOTS_RULE) !== null;
  const title = lotsBad ? `${verb} · 手数待改`
    : sizing.mode === "atr_risk" ? `${verb} · 按波动定手数（最多 ${most} 手）`
    : sizing.mode === "equity_percent" ? `${verb} · 按资金比例 ${sizing.equityPercent ?? "?"}% 权益（${sizing.maxLots ? `最多 ${sizing.maxLots} 手` : "手数不另设上限"}）`
    : `${verb} ${lots} 手${most > lots ? `（最多 ${most}）` : ""}`;
  const action: FlowBox = { id: "action", title,
    lines: form.pyramiding.on ? [`浮盈加仓最多 ${form.pyramiding.maxAdds ?? 1} 次`] : [], count: counted ? counted.summary.trades : null,
    side: direction === "both" ? undefined : direction };

  const reasons = new Map((counted?.exitReasons ?? []).map(item => [item.reason, item]));
  const exitCount = (id: FlowTarget) => {
    if (!counted) return { count: null, net: null };
    const items = (EXIT_REASONS[id] ?? []).map(reason => reasons.get(reason)).filter(Boolean);
    return { count: items.reduce((sum, item) => sum + item!.count, 0), net: items.length ? items.reduce((sum, item) => sum + item!.net, 0) : null };
  };
  const exits: FlowBox[] = [];
  const add = (id: FlowTarget, title: string, lines: string[] = [], pending = false) => exits.push({ id, title, lines, pending, ...(pending ? { count: null, net: null } : exitCount(id)) });
  if (missing.includes("止损") || missing.includes("单笔预算")) add("stop", "止损", ["待填"], true);
  else add("stop", stopText(form, Boolean(plan.spread)));
  if (form.take.mode === "ticks" && form.take.value != null) add("take", numberProblem(form.take.value, TAKE_TICKS_RULE) ? "止盈待改" : `止盈 ${form.take.value} 跳`);
  else if (plan.spread?.takeProfit) add("take", `价差止盈 ${plan.spread.takeProfit} 点`);
  if (form.trailing.on && form.trailing.value != null) add("trailing", `追踪 回撤 ${form.trailing.value}${form.trailing.mode === "percent" ? "%" : form.trailing.mode === "ticks" ? " 跳" : " 倍 ATR"}`);
  if (form.breakeven.on && form.breakeven.trigger != null) add("breakeven", `保本 浮盈 ${form.breakeven.trigger}${form.breakeven.unit === "ticks" ? " 跳" : " 倍 ATR"}后`);
  if (form.scaleOut.length) add("scaleOut", `分批止盈 ${form.scaleOut.length} 档`);
  const timeTiers = form.timeTakeProfit.filter(tier => tier.afterBars != null && tier.profit != null).length;
  if (timeTiers) add("timeTakeProfit", `按时间止盈 ${timeTiers} 档`);
  if (form.maxHoldBars) add("maxHold", `最长持仓 ${form.maxHoldBars} 根`);
  if (form.closeBeforeEnd.on) add("closeBeforeEnd", `收盘前 ${form.closeBeforeEnd.minutes ?? DEFAULTS.closeBeforeEndMinutes} 分${form.closeBeforeEnd.everySession ? "（每个时段）" : ""}`);
  const exitGroups = (["exitLong", "exitShort"] as GroupKey[]).filter(key => form[key].locked || form[key].rows.length);
  if (exitGroups.length) add("exitRules", "条件平仓", [exitGroups.map(key => `${SIDE_WORDS[key]} ${form[key].locked ? "（只读）" : `${form[key].rows.length} 条`}`).join(" · ")]);

  const cooldown = form.cooldownBars ?? DEFAULTS.cooldownBars;
  const drawdown = form.maxDrawdown.on && form.maxDrawdown.percent != null ? `本策略从最高点回撤 ${form.maxDrawdown.percent}% 停止并平仓` : "";
  const notes = [exits.length > 1 ? "先到先平" : "", drawdown, cooldown ? `平仓后冷却 ${cooldown} 根` : "平仓后不冷却",
    numberProblem(form.maxOrdersPerDay, MAX_ORDERS_RULE) ? "每天最多开仓次数待改" : `每天最多开 ${form.maxOrdersPerDay ?? DEFAULTS.maxOrdersPerDay} 次`, form.maxEntriesPerDirection ? `每个方向每天最多 ${form.maxEntriesPerDirection} 次` : "",
    counted?.summary.averageBarsHeld != null && counted.summary.trades ? holdingText(counted.summary.averageBarsHeld, counted.timeframeSeconds || timeframe, counted.coverage) : ""].filter(Boolean);
  return { basis: counted ? `近 ${counted.coverage.tradingDays} 个交易日` : null, trigger, entries, action, exits, notes };
}

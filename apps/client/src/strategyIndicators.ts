import type { Operand } from "./strategyApi";

/**
 * 指标选择器 (策略编辑卡第二版, 2026-10-08; the owner approved picker.html): every operand of the rule engine's
 * `Indicator` list (strategy_rules.py) in eight categories, each with one plain line, and the parameters each one
 * reads with the engine's own ranges — so a choice the engine would refuse is disabled here instead of failing on
 * save. Keep in step with strategy_rules.py: DEFAULT_PERIODS, WINDOWED, DI_MAX_PERIOD, TRIX_MAX_PERIOD,
 * MAX_BARS_AGO, DEFAULT_K, PIVOT_LEVELS, OI_QUADRANTS, SPREAD_INDICATORS and the Condition validator.
 */
export type IndicatorName = Operand["indicator"];
export type CategoryId = "common" | "trend" | "oscillator" | "channel" | "volume" | "candle" | "intraday" | "number";

export const CATEGORIES: { id: CategoryId; label: string }[] = [
  { id: "common", label: "常用" }, { id: "trend", label: "趋势" }, { id: "oscillator", label: "摆动" },
  { id: "channel", label: "通道与波动" }, { id: "volume", label: "成交量与持仓" }, { id: "candle", label: "K 线与价格" },
  { id: "intraday", label: "日内与昨日" }, { id: "number", label: "数值" }];

/** One entry of the list: one indicator, or a family whose lines (上轨 / 中轨 / 下轨 …) are picked below it. */
export interface PickerItem {
  id: string; label: string; desc: string; categories: CategoryId[];
  lines: { indicator: IndicatorName; label: string }[];
  /** Extra search words (English names, other spellings). */
  keywords?: string;
}

export const PICKER_ITEMS: PickerItem[] = [
  { id: "close", label: "收盘价", desc: "这根 K 线的收盘", categories: ["common", "candle"], lines: [{ indicator: "close", label: "收盘价" }], keywords: "close 价格" },
  { id: "ma", label: "均线 MA", desc: "N 根收盘均价", categories: ["common", "trend"], lines: [{ indicator: "ma", label: "均线" }], keywords: "ma sma 均线" },
  { id: "ema", label: "指数均线 EMA", desc: "近的权重大", categories: ["trend"], lines: [{ indicator: "ema", label: "EMA" }], keywords: "ema 均线" },
  { id: "macd", label: "MACD", desc: "快慢均线差（12, 26, 9）", categories: ["common", "trend"],
    lines: [{ indicator: "macd_dif", label: "DIF" }, { indicator: "macd_dea", label: "DEA" }, { indicator: "macd_hist", label: "柱" }], keywords: "macd dif dea 金叉 死叉" },
  { id: "supertrend", label: "SuperTrend 超级趋势", desc: "按 ATR 的趋势线", categories: ["trend"], lines: [{ indicator: "supertrend", label: "超级趋势" }], keywords: "supertrend 超级趋势" },
  { id: "sar", label: "SAR 抛物线", desc: "跟随止损点", categories: ["trend"], lines: [{ indicator: "sar", label: "SAR" }], keywords: "sar 抛物线 parabolic" },
  { id: "adx", label: "ADX 趋势强度", desc: "越高趋势越强", categories: ["trend"], lines: [{ indicator: "adx", label: "ADX" }], keywords: "adx dmi" },
  { id: "di", label: "+DI / −DI", desc: "多空力量", categories: ["trend"],
    lines: [{ indicator: "plus_di", label: "+DI" }, { indicator: "minus_di", label: "−DI" }], keywords: "dmi di pdi mdi" },
  { id: "trix", label: "TRIX", desc: "三重平滑均线的变化率", categories: ["trend"], lines: [{ indicator: "trix", label: "TRIX" }], keywords: "trix" },
  { id: "bbi", label: "BBI 多空指标", desc: "3/6/12/24 四条均线平均", categories: ["trend"], lines: [{ indicator: "bbi", label: "BBI" }], keywords: "bbi 多空" },
  { id: "rsi", label: "RSI", desc: "0–100，越高越强", categories: ["common", "oscillator"], lines: [{ indicator: "rsi", label: "RSI" }], keywords: "rsi 相对强弱 超买 超卖" },
  { id: "kd", label: "KD / KDJ", desc: "9, 3, 3；0–100", categories: ["common", "oscillator"],
    lines: [{ indicator: "kd_k", label: "K" }, { indicator: "kd_d", label: "D" }, { indicator: "kd_j", label: "J" }], keywords: "kd kdj stochastic 随机" },
  { id: "cci", label: "CCI", desc: "偏离平均价的程度", categories: ["oscillator"], lines: [{ indicator: "cci", label: "CCI" }], keywords: "cci 顺势" },
  { id: "williams_r", label: "威廉 %R", desc: "−100–0，近 0 偏强", categories: ["oscillator"], lines: [{ indicator: "williams_r", label: "威廉%R" }], keywords: "wr williams 威廉" },
  { id: "roc", label: "ROC 涨跌幅", desc: "较 N 根前涨跌 %", categories: ["oscillator"], lines: [{ indicator: "roc", label: "ROC" }], keywords: "roc 涨跌幅 动量" },
  { id: "bias", label: "乖离率 BIAS", desc: "收盘偏离均线 %", categories: ["oscillator"], lines: [{ indicator: "bias", label: "乖离率" }], keywords: "bias 乖离" },
  { id: "mfi", label: "MFI 资金流量", desc: "带成交量的 RSI", categories: ["oscillator", "volume"], lines: [{ indicator: "mfi", label: "MFI" }], keywords: "mfi 资金" },
  { id: "cmi", label: "CMI 震荡指数", desc: "0–100，越大越有趋势", categories: ["oscillator"], lines: [{ indicator: "cmi", label: "CMI" }], keywords: "cmi 震荡" },
  { id: "boll", label: "布林带", desc: "均线 ± k 倍标准差", categories: ["common", "channel"],
    lines: [{ indicator: "boll_up", label: "上轨" }, { indicator: "boll_mid", label: "中轨" }, { indicator: "boll_down", label: "下轨" }], keywords: "boll bollinger 布林" },
  { id: "keltner", label: "肯特纳通道", desc: "均线 ± k 倍 ATR", categories: ["channel"],
    lines: [{ indicator: "keltner_up", label: "上轨" }, { indicator: "keltner_mid", label: "中轨" }, { indicator: "keltner_down", label: "下轨" }], keywords: "keltner 肯特纳" },
  { id: "donchian", label: "前 N 根最高 / 最低", desc: "唐奇安通道，不含这根", categories: ["common", "channel"],
    lines: [{ indicator: "donchian_high", label: "最高" }, { indicator: "donchian_low", label: "最低" }], keywords: "donchian 唐奇安 突破 前高 前低" },
  { id: "atr", label: "ATR 平均波幅", desc: "N 根平均真实波动", categories: ["channel"], lines: [{ indicator: "atr", label: "ATR" }], keywords: "atr 波动" },
  { id: "range_nday", label: "N 日振幅", desc: "前 N 个交易日的波动幅度", categories: ["channel"], lines: [{ indicator: "range_nday", label: "N 日振幅" }], keywords: "range dual thrust 振幅" },
  { id: "volume", label: "成交量", desc: "这根 K 线的量", categories: ["volume"], lines: [{ indicator: "volume", label: "成交量" }], keywords: "volume vol 量" },
  { id: "volume_ratio", label: "放量倍数（比前 20 根均量）", desc: "这根的量 ÷ 前 20 根均量", categories: ["common", "volume"],
    lines: [{ indicator: "volume_ratio", label: "放量倍数" }], keywords: "volume ratio 量比 放量 均量" },
  { id: "oi_change", label: "持仓量变化", desc: "较 N 根前增减了多少手", categories: ["volume"], lines: [{ indicator: "oi_change", label: "持仓量变化" }], keywords: "oi 持仓 增仓 减仓" },
  { id: "oi_quadrant", label: "持仓四象限", desc: "价格与持仓一起看：多头增仓等", categories: ["volume"], lines: [{ indicator: "oi_quadrant", label: "持仓四象限" }], keywords: "oi 持仓 四象限 增仓 减仓" },
  { id: "open", label: "开盘价", desc: "这根 K 线的开盘", categories: ["candle"], lines: [{ indicator: "open", label: "开盘价" }], keywords: "open" },
  { id: "high", label: "最高价", desc: "这根 K 线的最高", categories: ["candle"], lines: [{ indicator: "high", label: "最高价" }], keywords: "high" },
  { id: "low", label: "最低价", desc: "这根 K 线的最低", categories: ["candle"], lines: [{ indicator: "low", label: "最低价" }], keywords: "low" },
  { id: "bar_range", label: "K 线振幅", desc: "最高 − 最低", categories: ["candle"], lines: [{ indicator: "bar_range", label: "K线振幅" }], keywords: "range 振幅" },
  { id: "body", label: "K 线实体", desc: "收盘 − 开盘，阳线为正", categories: ["candle"], lines: [{ indicator: "body", label: "K线实体" }], keywords: "body 阳线 阴线 实体" },
  { id: "shadow", label: "上影线 / 下影线", desc: "实体到最高 / 最低的距离", categories: ["candle"],
    lines: [{ indicator: "upper_shadow", label: "上影线" }, { indicator: "lower_shadow", label: "下影线" }], keywords: "shadow 影线" },
  { id: "prev_high", label: "昨高", desc: "上个交易日最高", categories: ["intraday"], lines: [{ indicator: "prev_high", label: "昨高" }], keywords: "昨日 最高" },
  { id: "prev_low", label: "昨低", desc: "上个交易日最低", categories: ["intraday"], lines: [{ indicator: "prev_low", label: "昨低" }], keywords: "昨日 最低" },
  { id: "prev_close", label: "昨收", desc: "上个交易日收盘", categories: ["intraday"], lines: [{ indicator: "prev_close", label: "昨收" }], keywords: "昨日 收盘" },
  { id: "today_open", label: "今开", desc: "今天的开盘价（夜盘算今天）", categories: ["intraday"], lines: [{ indicator: "today_open", label: "今开" }], keywords: "今日 开盘" },
  { id: "gap_percent", label: "开盘跳空 %", desc: "今开相对昨收", categories: ["intraday"], lines: [{ indicator: "gap_percent", label: "开盘跳空%" }], keywords: "gap 跳空" },
  { id: "opening_range", label: "开盘 N 根最高 / 最低", desc: "当天开盘区间", categories: ["intraday"],
    lines: [{ indicator: "opening_range_high", label: "最高" }, { indicator: "opening_range_low", label: "最低" }], keywords: "opening range 开盘区间 orb" },
  { id: "session", label: "当日最高 / 最低", desc: "今天到这根为止", categories: ["intraday"],
    lines: [{ indicator: "session_high", label: "最高" }, { indicator: "session_low", label: "最低" }], keywords: "当日 今日 最高 最低" },
  { id: "vwap", label: "分时均价 VWAP", desc: "今天按量加权的均价", categories: ["intraday", "volume"], lines: [{ indicator: "vwap", label: "分时均价" }], keywords: "vwap 均价 分时" },
  { id: "pivot", label: "枢轴位", desc: "按昨日高低收算的 P / R / S", categories: ["intraday"], lines: [{ indicator: "pivot", label: "枢轴" }], keywords: "pivot 枢轴 rbreaker r-breaker" },
  { id: "constant", label: "数值", desc: "固定的一个数", categories: ["number"], lines: [{ indicator: "constant", label: "数值" }], keywords: "number 数字 常数" },
];

const ITEM_OF = new Map<IndicatorName, PickerItem>(PICKER_ITEMS.flatMap(item => item.lines.map(line => [line.indicator, item] as const)));
export const itemOf = (indicator: IndicatorName | "") => indicator ? ITEM_OF.get(indicator) : undefined;
/** Every operand the picker reaches (the rule engine's list). */
export const PICKABLE: IndicatorName[] = [...ITEM_OF.keys()];

// -- what each operand reads (strategy_rules.py) ---------------------------------------------------

/** DEFAULT_PERIODS, with `ma` at 20 (the card's habit; the engine's historical default is 5). */
const DEFAULT_PERIODS: Partial<Record<IndicatorName, number>> = { ma: 20, ema: 20, atr: 14, donchian_high: 20, donchian_low: 20, boll_mid: 20,
  boll_up: 20, boll_down: 20, keltner_mid: 20, keltner_up: 20, keltner_down: 20, rsi: 14, supertrend: 10, range_nday: 5,
  opening_range_high: 1, opening_range_low: 1, cci: 20, adx: 14, plus_di: 14, minus_di: 14, oi_change: 1, oi_quadrant: 1,
  roc: 12, trix: 12, williams_r: 14, bias: 6, mfi: 14, cmi: 30 };
/** WINDOWED: a period of at least 2. */
const WINDOWED = new Set<IndicatorName>(["ma", "ema", "atr", "donchian_high", "donchian_low", "boll_mid", "boll_up", "boll_down",
  "keltner_mid", "keltner_up", "keltner_down", "rsi", "supertrend", "cci", "adx", "plus_di", "minus_di", "trix", "williams_r", "bias", "mfi", "cmi"]);
const PERIOD_MAX: Partial<Record<IndicatorName, number>> = { adx: 50, plus_di: 50, minus_di: 50, trix: 60 };
const K_DEFAULTS: Partial<Record<IndicatorName, number>> = { boll_up: 2, boll_down: 2, keltner_up: 1.5, keltner_down: 1.5, supertrend: 3 };
export const MAX_BARS_AGO = 50;
export const PIVOT_LEVELS: { value: number; label: string }[] = [
  { value: 3, label: "R3（突破买入价）" }, { value: 2, label: "R2（观察卖出价）" }, { value: 1, label: "R1（反转卖出价）" }, { value: 0, label: "P" },
  { value: -1, label: "S1（反转买入价）" }, { value: -2, label: "S2（观察买入价）" }, { value: -3, label: "S3（突破卖出价）" }];
export const OI_QUADRANTS: { value: number; label: string }[] = [
  { value: 1, label: "多头增仓" }, { value: 2, label: "空头增仓" }, { value: 3, label: "空头减仓" }, { value: 4, label: "多头减仓" }];
/** SPREAD_INDICATORS: what a 价差 plan may read (only the bars' open and close). */
const SPREAD_INDICATORS = new Set<IndicatorName>(["constant", "close", "open", "ma", "ema", "macd_dif", "macd_dea", "macd_hist",
  "boll_mid", "boll_up", "boll_down", "rsi", "roc", "bias", "trix", "bbi"]);

export const usesPeriod = (indicator: IndicatorName | "") => Boolean(indicator) && DEFAULT_PERIODS[indicator as IndicatorName] !== undefined;
export const defaultPeriod = (indicator: IndicatorName | "") => indicator ? DEFAULT_PERIODS[indicator as IndicatorName] ?? null : null;
export const usesK = (indicator: IndicatorName | "") => Boolean(indicator) && K_DEFAULTS[indicator as IndicatorName] !== undefined;
export const defaultK = (indicator: IndicatorName | "") => indicator ? K_DEFAULTS[indicator as IndicatorName] ?? null : null;
export const usesBasis = (indicator: IndicatorName | "") => typeof indicator === "string" && indicator.startsWith("keltner_");
export function periodRange(indicator: IndicatorName | ""): { min: number; max: number } {
  return { min: indicator && WINDOWED.has(indicator as IndicatorName) ? 2 : 1, max: (indicator && PERIOD_MAX[indicator as IndicatorName]) || 200 };
}
/** The period's words beside its box. */
export function periodLabel(indicator: IndicatorName | ""): { prefix: string; unit: string } {
  if (indicator === "range_nday") return { prefix: "前", unit: "个交易日" };
  if (indicator === "opening_range_high" || indicator === "opening_range_low") return { prefix: "开盘前", unit: "根" };
  if (indicator === "oi_change" || indicator === "oi_quadrant" || indicator === "roc") return { prefix: "比", unit: "根前" };
  return { prefix: "周期", unit: "根" };
}
export const spreadAllows = (indicator: IndicatorName) => SPREAD_INDICATORS.has(indicator);

// -- conditions -------------------------------------------------------------------------------------

export type Operator = "gt" | "gte" | "lt" | "lte" | "cross_up" | "cross_down" | "rising" | "falling" | "eq";
export const OPERATORS: { value: Operator; label: string }[] = [
  { value: "gt", label: "高于" }, { value: "lt", label: "低于" }, { value: "cross_up", label: "上穿" }, { value: "cross_down", label: "下穿" },
  { value: "gte", label: "不低于" }, { value: "lte", label: "不高于" }, { value: "rising", label: "上升" }, { value: "falling", label: "下降" },
  { value: "eq", label: "等于" }];
export const isCross = (op: Operator | "") => op === "cross_up" || op === "cross_down";
/** 上升 / 下降 compare with the value N bars ago: the right side is that N (a number 1–20). */
export const isTrend = (op: Operator | "") => op === "rising" || op === "falling";

/**
 * Why a comparison cannot be chosen with this left side (null = it can): 持仓四象限 only takes 等于 and 等于 only
 * takes 持仓四象限 (the engine's Condition validator); 交叉 cannot be required on N bars in a row.
 */
export function operatorProblem(op: Operator, left: IndicatorName | "", consecutive = 1, stateOnly = false): string | null {
  if (stateOnly && !["gt", "gte", "lt", "lte"].includes(op)) return "有方向确认时只看当前状态：高于 / 低于 / 不低于 / 不高于";
  if (left === "oi_quadrant" && op !== "eq") return "持仓四象限只能用“等于”";
  if (op === "eq" && left !== "oi_quadrant") return "“等于”只用于持仓四象限";
  if (isCross(op) && consecutive > 1) return "交叉是一次性事件，不能要求连续 N 根";
  return null;
}

/** Why an operand cannot be chosen on this side (null = it can). */
export function indicatorProblem(indicator: IndicatorName, side: "left" | "right", context: { spread?: boolean; op?: Operator | "" } = {}): string | null {
  if (indicator === "constant" && side === "left") return "左边要是行情指标";
  if (side === "right" && indicator !== "constant" && (isTrend(context.op ?? "") || context.op === "eq")) return context.op === "eq" ? "四象限的右边是哪一种" : "上升 / 下降的右边是几根前";
  if (side === "right" && indicator === "oi_quadrant") return "持仓四象限只放左边";
  if (context.spread && !spreadAllows(indicator)) return "价差策略只能用只看开盘、收盘价的指标";
  return null;
}

/** The modifiers' ranges (Condition: withinBars, consecutive, minCount 1–20; minCount ≤ withinBars). */
export const MODIFIER_MAX = 20;

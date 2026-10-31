import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Typed numbers (2026-10-13, #789 / #745 / #755 / #759): one rule and one reading for every box where
 * a person types an amount — 下单票的手数 / 委托价 / 止损止盈 / 目标净持仓, 策略编辑卡的手数 / 开仓次数 /
 * 止损. A box may sit empty or half-typed while the person edits it; what it holds is read against its
 * rule, and a value that breaks the rule is reported where it is typed and blocks the action. The box
 * never quietly turns into another number (a cleared 手数 used to snap back to 1, so typing 5 gave 15).
 */
export type NumberRule = {
  /** 中文名, the subject of the reason (手数, 委托价, 每天最多开仓次数…). */
  label: string;
  min?: number;
  max?: number;
  /** `min` itself is not allowed (价格、倍数 must be above 0). */
  minExclusive?: boolean;
  integer?: boolean;
  unit?: string;
  /** Empty is a valid answer (不设 / 用默认); otherwise an empty box is a problem. */
  optional?: boolean;
  /** Replaces the generated range wording, e.g. 跌停 780.68 到涨停 1,034.86 之间. */
  rangeText?: string;
};

const NUMBER_TEXT = /^[+-]?(\d+\.?\d*|\.\d+)$/;

/** The allowed range in words: "1–20 的整数", "大于 0、最多 20", "大于 0". */
export function rangeHint(rule: NumberRule): string {
  if (rule.rangeText) return rule.rangeText;
  const { min, max, unit = "" } = rule;
  if (rule.integer) {
    if (min !== undefined && max !== undefined) return `${min}–${max}${unit ? ` ${unit}` : ""}的整数`;
    if (min !== undefined) return `不小于 ${min} 的整数`;
    return "整数";
  }
  const low = min === undefined ? "" : rule.minExclusive ? `大于 ${min}` : `不小于 ${min}`;
  const high = max === undefined ? "" : `最多 ${max}${unit}`;
  return [low, high].filter(Boolean).join("、") || "数字";
}

/** Why `value` does not satisfy `rule`, in one Chinese line; null when it does. */
export function numberProblem(value: number | null | undefined, rule: NumberRule): string | null {
  if (value === null || value === undefined) return rule.optional ? null : `请输入${rule.label}`;
  if (!Number.isFinite(value)) return `${rule.label}要填数字`;
  const { min, max } = rule;
  const outside = (rule.integer && !Number.isInteger(value))
    || (min !== undefined && (rule.minExclusive ? value <= min : value < min))
    || (max !== undefined && value > max);
  if (!outside) return null;
  if (rule.rangeText) return `${rule.label}须在${rule.rangeText}`;
  return rule.integer ? `${rule.label}须是 ${rangeHint(rule)}` : `${rule.label}须${rangeHint(rule)}`;
}

/** A box's text read against its rule. `badInput`: the browser holds text it cannot read as a number. */
export function readNumber(raw: string, rule: NumberRule, badInput = false): { value: number | null; problem: string | null } {
  const text = raw.trim();
  if (badInput || (text !== "" && !NUMBER_TEXT.test(text))) return { value: null, problem: `${rule.label}要填数字` };
  if (text === "") return { value: null, problem: numberProblem(null, rule) };
  const value = Number(text);
  return { value, problem: numberProblem(value, rule) };
}

/**
 * A box that owns its text: the person may clear it or type a half number; only a value that satisfies
 * the rule reaches `onValue`. A change from outside (a stepper, a draft, a reset) replaces the text unless
 * the text already reads as that value. Leaving the box tidies a valid entry (05 → 5) and keeps an invalid
 * one as typed, with its reason.
 */
export function useNumberText(value: number | null, rule: NumberRule, onValue: (value: number | null) => void) {
  const [text, setText] = useState(value === null ? "" : String(value));
  const [badInput, setBadInput] = useState(false);
  useEffect(() => {
    setText(current => {
      const read = readNumber(current, rule);
      return read.problem === null && read.value === value ? current : value === null ? "" : String(value);
    });
    setBadInput(false);
    // Only an outside change of the value resets the text; the rule is re-read on every render.
  }, [value]);
  const read = readNumber(text, rule, badInput);
  // A changed rule can make the kept text valid (60 手 over the open limit, then 平仓 mode): the value the
  // action sends must follow what the box shows. An outside change of the value wins over the kept text.
  const ruleKey = `${rule.min}|${rule.max}|${rule.minExclusive}|${rule.integer}|${rule.optional}`;
  const seen = useRef({ ruleKey, value });
  useEffect(() => {
    const previous = seen.current;
    seen.current = { ruleKey, value };
    if (previous.ruleKey === ruleKey || previous.value !== value) return;
    if (read.problem === null && read.value !== value) onValue(read.value);
  }, [ruleKey, value, read.problem, read.value, onValue]);
  const onChange = useCallback((event: { target: { value: string; validity?: { badInput?: boolean } } }) => {
    const raw = event.target.value;
    const bad = Boolean(event.target.validity?.badInput);
    setText(raw);
    setBadInput(bad);
    const next = readNumber(raw, rule, bad);
    if (next.problem === null && next.value !== value) onValue(next.value);
  }, [rule, value, onValue]);
  const onBlur = useCallback(() => {
    if (read.problem === null && !badInput) setText(read.value === null ? "" : String(read.value));
  }, [read.problem, read.value, badInput]);
  /** Put `next` in the box (stepper buttons): the text and the value move together. */
  const replace = useCallback((next: number | null) => {
    setText(next === null ? "" : String(next));
    setBadInput(false);
    if (next !== value) onValue(next);
  }, [value, onValue]);
  return { text, problem: read.problem, value: read.problem === null ? read.value : null, onChange, onBlur, replace };
}

/** 止损 / 止盈 price boxes (下单票, 持仓行保护): empty = 不设; anything typed must be a price above 0. */
export const STOP_PRICE_RULE: NumberRule = { label: "止损", min: 0, minExclusive: true, optional: true };
export const TAKE_PRICE_RULE: NumberRule = { label: "止盈", min: 0, minExclusive: true, optional: true };

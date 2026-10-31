import { describe, expect, test } from "vitest";
import { numberProblem, readNumber, type NumberRule } from "./numberField";

const LOTS: NumberRule = { label: "手数", min: 1, max: 1000, integer: true, unit: "手" };

describe("numberField: one reading for every typed amount (#789)", () => {
  test("a valid entry reads as its value; leading zeros and a trailing point are fine", () => {
    expect(readNumber("5", LOTS)).toEqual({ value: 5, problem: null });
    expect(readNumber(" 05 ", LOTS)).toEqual({ value: 5, problem: null });
    expect(readNumber("5.", LOTS)).toEqual({ value: 5, problem: null });
  });

  test("empty, not a number, a fraction and out of range each say why — never another number", () => {
    expect(readNumber("", LOTS)).toEqual({ value: null, problem: "请输入手数" });
    expect(readNumber("abc", LOTS)).toEqual({ value: null, problem: "手数要填数字" });
    expect(readNumber("1e3", LOTS).problem).toBe("手数要填数字");
    expect(readNumber("", LOTS, true).problem).toBe("手数要填数字");  // the browser holds text it cannot read
    expect(readNumber("1.5", LOTS).problem).toBe("手数须是 1–1000 手的整数");
    expect(readNumber("0", LOTS).problem).toBe("手数须是 1–1000 手的整数");
    expect(readNumber("-1", LOTS).problem).toBe("手数须是 1–1000 手的整数");
  });

  test("optional boxes accept empty; exclusive minimum and custom range wording", () => {
    const stop: NumberRule = { label: "止损", min: 0, minExclusive: true, optional: true };
    expect(readNumber("", stop)).toEqual({ value: null, problem: null });
    expect(numberProblem(0, stop)).toBe("止损须大于 0");
    const atr: NumberRule = { label: "止损 ATR 倍数", min: 0, minExclusive: true, max: 20 };
    expect(numberProblem(21, atr)).toBe("止损 ATR 倍数须大于 0、最多 20");
    const price: NumberRule = { label: "委托价", min: 700, max: 900, rangeText: "跌停 700 到涨停 900 之间" };
    expect(numberProblem(901, price)).toBe("委托价须在跌停 700 到涨停 900 之间");
    expect(numberProblem(800, price)).toBeNull();
  });
});

import { describe, expect, test } from "vitest";
import { contractPriceFormat, contractTickmarks, formatCciPrice, formatIntegerPrice, formatMacdPrice, trimPriceZeros } from "./chartPriceFormat";

describe("axis formatting", () => {
  test.each([
    ["29600.00", "29600"], ["29474.50", "29474.5"], ["80.00", "80"],
    ["0.000", "0"], ["-10.000", "-10"], ["-0.000", "0"], ["100", "100"],
  ])("removes fractional zeros from %s", (value, expected) => {
    expect(trimPriceZeros(value)).toBe(expected);
  });

  test("tick labels trim zeros while last-price and free-crosshair labels keep the contract grid and precision", () => {
    const quarter = contractPriceFormat(0.25);
    expect(quarter.formatter(29474.33)).toBe("29474.25");
    expect(quarter.formatter(29474.5)).toBe("29474.50");
    expect(quarter.formatter(29600)).toBe("29600.00");
    expect(contractTickmarks(0.25)([29600, 29474.5, -10, 0])).toEqual(["29600", "29474.5", "-10", "0"]);
    expect(contractPriceFormat(1).formatter(29474.33)).toBe("29474");
    expect(contractPriceFormat(0.005).formatter(1.013)).toBe("1.015");
    expect(contractPriceFormat(null).formatter(1.234)).toBe("1.23");
    expect(contractPriceFormat(0).formatter(1)).toBe("1.00");
  });

  test.each([[12.345, "12"], [-12.345, "-12"], [10, "10"], [1.234, "1.2"],
    [-1.2, "-1.2"], [0.1234, "0.12"], [0.01234, "0.012"], [0.1, "0.1"], [0, "0"], [-0.0001, "0"]])(
    "MACD %s follows its magnitude without trailing zeros", (value, expected) => {
      expect(formatMacdPrice(value)).toBe(expected);
    });

  test("oscillator labels round KD/RSI to integers and trim CCI decimals", () => {
    expect(formatIntegerPrice(79.51)).toBe("80");
    expect(formatIntegerPrice(-0.2)).toBe("0");
    expect(formatCciPrice(-10)).toBe("-10");
    expect(formatCciPrice(12.34)).toBe("12.3");
  });
});

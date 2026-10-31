// @vitest-environment node
import { CandlestickChart, Landmark, Pickaxe } from "lucide-react";
import { expect, test } from "vitest";
import { productIcon } from "./productIcons";
import products from "./productCatalogProducts.fixture.json";

// Every (exchange, product) the live TqKq catalog listed on 2026-09-13: each gets a real glyph, none fall to the default.
test("every product in the catalog snapshot has its own icon; unknown products fall back to the candlestick", () => {
  const missing = (products as [string, string][]).filter(([exchange, product]) => productIcon(product, exchange) === CandlestickChart);
  expect(missing).toEqual([]);
  expect(productIcon("xyz", "SHFE")).toBe(CandlestickChart);
  expect(productIcon(undefined)).toBe(CandlestickChart);
});

test("product ids are matched case-insensitively and foreign ids on the part after the dot; the exchange breaks ties", () => {
  expect(productIcon("RB", "SHFE")).toBe(productIcon("rb", "SHFE"));
  expect(productIcon("CBOT.ZN", "KQD")).toBe(Landmark);
  expect(productIcon("TF", "CFFEX")).toBe(Landmark);
  expect(productIcon("SGX.TF", "KQD")).toBe(Pickaxe);
});

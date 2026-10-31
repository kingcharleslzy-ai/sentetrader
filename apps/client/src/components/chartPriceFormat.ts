import { snapToTick } from "../chartDrawingModel";
import { decimalsForTick } from "./futures/support";

/** Only fractional trailing zeros go away; integer place values stay intact. */
export function trimPriceZeros(value: string): string {
  const trimmed = value.includes(".") ? value.replace(/0+$/, "").replace(/\.$/, "") : value;
  return Number(trimmed) === 0 ? "0" : trimmed;
}

export function contractPriceFormat(tick: number | null | undefined) {
  const minMove = tick && Number.isFinite(tick) && tick > 0 ? tick : 0.01;
  const precision = decimalsForTick(minMove);
  return {
    type: "custom" as const,
    minMove,
    // The free crosshair must use the same tick grid as drawings and price selection.
    formatter: (value: number) => snapToTick(value, minMove).toFixed(precision),
  };
}

/** Tick formatting is separate from the fixed-precision last-price/crosshair labels. */
export function contractTickmarks(tick: number | null | undefined) {
  const precision = decimalsForTick(tick && tick > 0 ? tick : 0.01);
  return (values: number[]) => values.map(value => trimPriceZeros(value.toFixed(precision)));
}

export function formatMacdPrice(value: number): string {
  const magnitude = Math.abs(value);
  const precision = magnitude >= 10 ? 0 : magnitude >= 1 ? 1 : magnitude >= 0.1 ? 2 : 3;
  return trimPriceZeros(value.toFixed(precision));
}

export function formatIntegerPrice(value: number): string {
  return trimPriceZeros(value.toFixed(0));
}

export function formatCciPrice(value: number): string {
  return trimPriceZeros(value.toFixed(1));
}

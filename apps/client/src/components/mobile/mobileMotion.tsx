import { useState } from "react";

/**
 * 价格跳动 (手机版, 2026-10-06): each change of a shown price flashes the soft market colour of its
 * direction behind the figure once. The value names the direction and alternates a/b, so CSS can
 * restart the animation on every tick without remounting the figure. A new `identity` (another
 * contract) starts over without a flash.
 */
export function usePriceFlash(price: number | null | undefined, identity?: string): string | undefined {
  const [state, setState] = useState<{ price: number | null | undefined; identity?: string; flash?: string; count: number }>({ price, identity, count: 0 });
  if (state.identity !== identity) {
    // Another contract: its first price is not a tick.
    setState({ price, identity, count: state.count });
  } else if (state.price !== price) {
    const before = state.price;
    const moved = typeof before === "number" && typeof price === "number" && Number.isFinite(before) && Number.isFinite(price) && before !== price;
    const count = moved ? state.count + 1 : state.count;
    setState({ price, identity, count, flash: moved ? `${price > before ? "up" : "down"}-${count % 2 ? "a" : "b"}` : state.flash });
  }
  return state.flash;
}

/** Derived from a growing count: a new key each time it rises, so the element replays its pop. */
export function useRiseKey(value: number): number {
  const [state, setState] = useState({ value, key: 0 });
  if (state.value !== value) setState({ value, key: value > state.value ? state.key + 1 : state.key });
  return state.key;
}

/**
 * 数字逐位滚动 (手机版第二轮, 2026-11-01): a shown figure whose text changes rolls only the digits that changed — the old
 * digit slides up and out, the new one in from below, over --dur-2 (which reduced motion zeroes: the new text simply
 * shows). Texts are compared from the right, so 9,999 → 10,000 rolls each digit that moved; a new `identity` (another
 * contract) swaps without rolling. The old digit is a CSS attribute, so the element's text is always the current value.
 */
export function RollingText({ text, identity }: { text: string; identity?: string }) {
  const [state, setState] = useState<{ text: string; identity?: string; from: string; count: number }>({ text, identity, from: text, count: 0 });
  if (state.identity !== identity) setState({ text, identity, from: text, count: state.count });
  else if (state.text !== text) setState({ text, identity, from: state.text, count: state.count + 1 });
  const offset = state.from.length - text.length;
  return <>{[...text].map((char, index) => {
    const before = state.from[index + offset];
    const roll = /\d/.test(char) && before !== undefined && before !== char && /\d/.test(before);
    return roll ? <span key={`${index}-${state.count}`} className="fdm-roll"><span data-from={before}>{char}</span></span> : char;
  })}</>;
}

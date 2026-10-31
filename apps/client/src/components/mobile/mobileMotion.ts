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

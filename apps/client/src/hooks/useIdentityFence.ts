import { useEffect, useRef } from "react";
import { readIdentityGeneration, useReadIdentity } from "../runtimeClient";

/** Per-channel read/write ordering: a read only lands if no newer read or write started meanwhile. */
export interface FenceChannel {
  /** Mark the start of a read; the returned check is true only while it is still the latest and no write intervened. */
  beginRead(): () => boolean;
  /** Record a local write so in-flight reads started before it are discarded. */
  noteWrite(): void;
}

export interface FenceContext {
  readonly identity: number;
}

export interface IdentityFence {
  /** Stable object for the current authenticated workspace; state tagged with it is discarded when it changes. */
  readonly context: FenceContext;
  /** True while this fence is the mounted workspace and the login identity has not changed. */
  isCurrent(): boolean;
  /** Ordering for one named read/write channel (orders, execution…). */
  channel(name: string): FenceChannel;
}

class Fence implements IdentityFence {
  readonly context: FenceContext;
  private readonly counters = new Map<string, { read: number; write: number }>();
  constructor(identity: number, private readonly latest: { current: Fence | undefined }) {
    this.context = { identity };
  }
  // Arrow properties: callers pass these around detached from the fence.
  isCurrent = (): boolean =>
    this.latest.current === this && this.context.identity === readIdentityGeneration();
  channel = (name: string): FenceChannel => {
    const counter = this.counters.get(name) ?? { read: 0, write: 0 };
    this.counters.set(name, counter);
    return {
      beginRead: () => {
        const sequence = ++counter.read;
        const write = counter.write;
        return () => this.isCurrent() && sequence === counter.read && write === counter.write;
      },
      noteWrite: () => { counter.write += 1; },
    };
  };
}

/**
 * One fence per authenticated workspace. Any async write-back into React state
 * must check `fence.isCurrent()` (or a channel read check) after awaiting, so a
 * response from a previous login, runtime or session never lands in the next.
 */
export function useIdentityFence(scope: readonly unknown[]): IdentityFence {
  const identity = useReadIdentity();
  const latest = useRef<Fence | undefined>(undefined);
  const memo = useRef<{ identity: number; scope: readonly unknown[]; fence: Fence } | undefined>(undefined);
  const previous = memo.current;
  if (!previous || previous.identity !== identity || previous.scope.length !== scope.length
      || previous.scope.some((item, index) => !Object.is(item, scope[index]))) {
    memo.current = { identity, scope: [...scope], fence: new Fence(identity, latest) };
  }
  const fence = memo.current!.fence;
  latest.current = fence;
  // After unmount nothing is current: a late response must not treat the last
  // fence as live even though React would drop the setState anyway.
  useEffect(() => () => { latest.current = undefined; }, []);
  return fence;
}

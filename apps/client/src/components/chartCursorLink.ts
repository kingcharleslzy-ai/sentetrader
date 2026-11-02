import { createContext, useCallback, useContext, useEffect, useId, useState } from "react";

/**
 * 多图 · 同步十字线 (2026-11-02): one hovered bar time shared by the charts of a grid. Each chart publishes its own
 * cursor time and shows the others' (never its own echo); outside a provider nothing is shared.
 */
export interface CursorLink {
  set: (source: string, time: number | undefined) => void;
  get: () => { source?: string; time?: number };
  subscribe: (listener: () => void) => () => void;
}

export function createCursorLink(): CursorLink {
  let value: { source?: string; time?: number } = {};
  const listeners = new Set<() => void>();
  return {
    set: (source, time) => {
      if (value.time === time && (value.source === source || time === undefined)) return;
      // A chart clearing its cursor never clears the one another chart has since taken over.
      if (time === undefined && value.source !== source) return;
      value = { source, time };
      listeners.forEach(listener => listener());
    },
    get: () => value,
    subscribe: listener => { listeners.add(listener); return () => { listeners.delete(listener); }; },
  };
}

export const CursorLinkContext = createContext<CursorLink | null>(null);

export function useCursorLink(): { linkedTime: number | undefined; publish: (time: number | undefined) => void } {
  const link = useContext(CursorLinkContext);
  const id = useId();
  const [linked, setLinked] = useState<number>();
  useEffect(() => {
    if (!link) { setLinked(undefined); return; }
    return link.subscribe(() => { const value = link.get(); setLinked(value.source === id ? undefined : value.time); });
  }, [link, id]);
  const publish = useCallback((time: number | undefined) => link?.set(id, time), [link, id]);
  return { linkedTime: link ? linked : undefined, publish };
}

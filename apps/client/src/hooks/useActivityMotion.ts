import { useEffect, useRef, useState } from "react";
import type { ActivitySnapshot } from "../runtimeClient";

// Freshness expires even when polling stops delivering snapshots.
export function useActivityFresh(activity: ActivitySnapshot | undefined, error: string) {
  const [fresh, setFresh] = useState(false);
  useEffect(() => {
    const remaining = 15_000 - (activity?.ageSeconds ?? 0) * 1000;
    const valid = Boolean(activity && !error && activity.dataStatus === "normal" && remaining > 0);
    setFresh(valid);
    if (!valid) return;
    const timer = setTimeout(() => setFresh(false), remaining);
    return () => clearTimeout(timer);
  }, [activity, error]);
  return fresh && !error && activity?.dataStatus === "normal";
}

export function useFillFeedback(activity: ActivitySnapshot | undefined, scope: string, enabled: boolean) {
  const previous = useRef<{scope: string; fills: Map<string, number>} | undefined>(undefined);
  const [changed, setChanged] = useState<Set<string>>(new Set());
  useEffect(() => {
    if (!enabled || !activity) { previous.current = undefined; setChanged(new Set()); return; }
    const fills = new Map(activity.fills.map(fill => [`${fill.clientOrderId}:${fill.tradeId}`, fill.volume]));
    const ids = new Set<string>();
    if (previous.current?.scope !== scope) setChanged(new Set());
    if (previous.current?.scope === scope) {
      for (const fill of activity.fills) {
        if (fill.volume > (previous.current.fills.get(`${fill.clientOrderId}:${fill.tradeId}`) ?? 0)) ids.add(fill.clientOrderId);
      }
    }
    previous.current = {scope, fills};
    if (ids.size) setChanged(ids);
  }, [activity, scope, enabled]);
  useEffect(() => {
    if (!changed.size) return;
    const timer = setTimeout(() => setChanged(new Set()), 650);
    return () => clearTimeout(timer);
  }, [changed]);
  return changed;
}

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ResearchJudgmentCard as Judgment } from "@ai-trader/ui-events";
import { requestJson } from "../../api";
import { LONG_READ_TIMEOUT_MS } from "../../requestTimeout";
import type { DeskReport } from "../ResearchDesk";
import { deskJudgmentCard } from "./feedItems";

const DESK_REFRESH_MS = 10 * 60 * 1000;
const DESK_PENDING_MS = 60 * 1000;
const PENDING_READ_FAILED = "今日判断生成中；刚才没读到最新状态，1 分钟后再查。";

type DeskState = { card: Judgment | null; failed: boolean; loading: boolean };
const DESK_EMPTY: DeskState = { card: null, failed: false, loading: false };
const DESK_LOADING: DeskState = { card: null, failed: false, loading: true };

/** The research desk's judgment of each product given (the 调研 tab's own read), as 判断卡, in the
 *  order given; `loading` until a product's first read returns (读取中, not yet 暂无); `failed` while
 *  a product's last read failed with nothing read yet (the next read retries). The desk keeps one shared judgment per product, so reading more products starts no
 *  per-user generation. `reload` reads every product again now (下拉刷新); what was read stays
 *  until the new read lands, and a failed reload keeps it. */
export function useDeskJudgments(baseUrl: string | undefined, symbols: readonly string[]): { items: DeskState[]; reload: () => Promise<void> } {
  const key = symbols.join(",");
  const [states, setStates] = useState<Record<string, DeskState>>({});
  const reader = useRef<() => Promise<void>>(async () => undefined);
  useEffect(() => {
    setStates({});
    const list = key ? key.split(",") : [];
    reader.current = async () => undefined;
    if (!baseUrl || !list.length) return;
    let active = true;
    const timers = new Map<string, number>();
    const last = new Map<string, Judgment>();
    const read = async (symbol: string) => {
      let next: Judgment | null = null;
      let failed = false;
      try {
        next = deskJudgmentCard(await requestJson<DeskReport>(`${baseUrl}/api/research/desk?symbol=${encodeURIComponent(symbol)}`, { timeoutMs: LONG_READ_TIMEOUT_MS }));
        last.set(symbol, next);
        if (active) setStates(current => ({ ...current, [symbol]: { card: next, failed: false, loading: false } }));
      } catch {
        // The feed shows what it has and the next read retries; a judgment still being written says the check
        // failed instead of reading “生成中” for ten minutes (#516).
        failed = true;
        if (active) setStates(current => {
          const card = current[symbol]?.card;
          if (!card) return { ...current, [symbol]: { card: null, failed: true, loading: false } };
          return { ...current, [symbol]: { card: card.status === "pending" ? { ...card, note: PENDING_READ_FAILED } : card, failed: true, loading: false } };
        });
      }
      if (!active) return;
      // A judgment being written, or a failed read of one (or of nothing yet), is looked at again soon.
      const kept = next ?? last.get(symbol);
      const soon = next?.status === "pending" || (failed && (!kept || kept.status === "pending"));
      window.clearTimeout(timers.get(symbol));
      timers.set(symbol, window.setTimeout(() => void read(symbol), soon ? DESK_PENDING_MS : DESK_REFRESH_MS));
    };
    reader.current = async () => { await Promise.all(list.map(read)); };
    list.forEach(symbol => void read(symbol));
    return () => { active = false; timers.forEach(timer => window.clearTimeout(timer)); };
  }, [baseUrl, key]);
  const reading = Boolean(baseUrl);
  const items = useMemo(() => (key ? key.split(",") : []).map(symbol => states[symbol] ?? (reading ? DESK_LOADING : DESK_EMPTY)), [key, states, reading]);
  const reload = useCallback(() => reader.current(), []);
  return useMemo(() => ({ items, reload }), [items, reload]);
}

/** The research desk's judgment of the shown product, as a 判断卡. */
export function useDeskJudgment(baseUrl: string | undefined, symbol: string | undefined): DeskState & { reload: () => Promise<void> } {
  const { items, reload } = useDeskJudgments(baseUrl, symbol ? [symbol] : []);
  return { ...(items[0] ?? DESK_EMPTY), reload };
}

/** A card-shaped placeholder while the first read runs (the shimmer stops under reduced motion). */
export function CardSkeleton({ label }: { label: string }) {
  return <div className="fdm-skeleton" role="status" aria-label={label}><i /><i /><i /></div>;
}

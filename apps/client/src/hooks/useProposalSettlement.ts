import { useEffect, useRef, useState, type Dispatch } from "react";
import { requestJson } from "../api";
import { settledProposalEvent, staleOpenProposals, type ConversationAction, type ConversationState } from "../state/conversation";

/** The waits before reading an unsettled card again (a 5xx, a timeout, a record still waiting); then it stays as it is. */
export const SETTLE_RETRY_MS = [5_000, 15_000, 45_000, 120_000, 300_000] as const;
/** Cards read in one request (the Runtime's limit for GET /api/proposals?ids=…). */
const SETTLE_BATCH = 100;

interface Tracker {
  scope: string;
  /** Read right now, by this connection's reads. */
  reading: Set<string>;
  /** Settled from its record, or unreadable for good (another account's card, an older Runtime): never read again. */
  done: Set<string>;
  /** Read again from `at` (ms); `attempts` so far. */
  retry: Map<string, { attempts: number; at: number }>;
  timers: Set<ReturnType<typeof setTimeout>>;
  /** The reads of the current connection: aborted when the stream drops or the tracker closes. */
  reads: AbortController;
}

const tracker = (scope: string): Tracker => ({ scope, reading: new Set(), done: new Set(), retry: new Map(), timers: new Set(),
  reads: new AbortController() });

function close(entry: Tracker) {
  for (const timer of entry.timers) clearTimeout(timer);
  entry.timers.clear();
  entry.reads.abort();
}

/** The stream dropped or came back: the reads in flight belong to the old connection — abort them, read anew. */
function restartReads(entry: Tracker) {
  entry.reads.abort();
  entry.reads = new AbortController();
  entry.reading.clear();
}

/**
 * 历史卡的结局 (#932 P2, R4 2026-10-19): an order card replayed as waiting long after its confirm window reads its own
 * record (GET /api/proposals?ids=…, every due card in one request) and settles as 已发送 / 已过期 / 已作废 / 已放弃 like a live proposal.result would.
 * Only the server's outcome counts — never a confirm sent again, never a local clock calling it rejected. A read in
 * flight, a card settled and a read that failed are kept apart: a 5xx, a timeout or a record still waiting is read again
 * after a bounded backoff; a card the answer leaves out (not this user's, gone) is final. A dropped stream aborts the reads in flight and the reconnect reads at
 * once. Every read belongs to its Runtime, session and connection: an answer arriving after a switch or a reconnect
 * writes nothing.
 */
export function useProposalSettlement({ state, sessionId, connected, baseUrl, dispatch }: {
  state: ConversationState;
  sessionId: string | undefined;
  connected: boolean;
  baseUrl: string;
  dispatch: Dispatch<ConversationAction>;
}) {
  const current = useRef<Tracker>(tracker(""));
  const [wake, setWake] = useState(0);
  const wasConnected = useRef(connected);

  useEffect(() => () => close(current.current), []);
  useEffect(() => {
    const scope = `${baseUrl}|${sessionId ?? ""}`;
    if (current.current.scope !== scope) {
      close(current.current);
      current.current = tracker(scope);
    }
    const entry = current.current;
    if (connected !== wasConnected.current) {
      restartReads(entry);
      if (connected) for (const wait of entry.retry.values()) wait.at = 0;   // the stream is back: read now
    }
    wasConnected.current = connected;
    if (!sessionId || !connected) return;
    const now = Date.now();
    const due = staleOpenProposals(state, now)
      .filter(id => !entry.reading.has(id) && !entry.done.has(id) && (entry.retry.get(id)?.at ?? 0) <= now);
    if (!due.length) return;
    for (const id of due) entry.reading.add(id);
    const reads = entry.reads;
    const live = () => current.current === entry && entry.reads === reads;
    const later = (id: string) => {
      const attempts = (entry.retry.get(id)?.attempts ?? 0) + 1;
      if (attempts > SETTLE_RETRY_MS.length) {
        entry.retry.delete(id);
        entry.done.add(id);
        return;
      }
      const delay = SETTLE_RETRY_MS[attempts - 1];
      entry.retry.set(id, { attempts, at: Date.now() + delay });
      const timer = setTimeout(() => { entry.timers.delete(timer); if (current.current === entry) setWake(value => value + 1); }, delay);
      entry.timers.add(timer);
    };
    void (async () => {
      // One read for every due card (GET /api/proposals?ids=…, ≤ 100 a request), not one request per card.
      for (let start = 0; start < due.length; start += SETTLE_BATCH) {
        if (!live()) return;
        const ids = due.slice(start, start + SETTLE_BATCH);
        try {
          const query = ids.map(id => `ids=${encodeURIComponent(id)}`).join("&");
          const { items } = await requestJson<{ items: { id?: string; status?: string; payload?: Record<string, unknown> }[] }>(
            `${baseUrl}/api/proposals?${query}`, { signal: reads.signal });
          if (!live()) return;   // another Runtime, session or connection: the late answer writes nothing
          const records = new Map(items.map(record => [record.id, record]));
          for (const id of ids) {
            const record = records.get(id);
            const event = record && settledProposalEvent(sessionId, record);
            if (event) dispatch({ type: "event", event });
            if (event || !record) {                    // settled, or not this user's / gone: never read again
              entry.retry.delete(id);
              entry.done.add(id);
            } else later(id);                          // still waiting on the server: read again later
          }
        } catch (error) {
          if (!live()) return;
          const status = (error as { status?: number }).status;
          for (const id of ids) {
            if (status === 403 || status === 404) { entry.retry.delete(id); entry.done.add(id); } else later(id);
          }
        } finally {
          if (live()) for (const id of ids) entry.reading.delete(id);
        }
      }
    })();
  }, [state, sessionId, connected, baseUrl, wake, dispatch]);
}

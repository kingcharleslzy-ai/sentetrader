import { useCallback, useEffect, useRef, useState } from "react";
import { fetchChatSuggestions, type ChatSuggestionRequest, type ChatSuggestionView } from "../api";
import { useReadIdentity } from "../runtimeClient";

/** The place named over the suggestions in an empty conversation: “在{地点}可以这样问”. */
export const CHAT_PLACE_LABELS: Record<ChatSuggestionView, string> = {
  trade: "K 线图", board: "报价总表", review: "复盘页", strategy: "策略面板", positions: "持仓面板",
};

/** How long the contract or view must stay put before the AI is asked again. */
export const SUGGESTION_DEBOUNCE_MS = 1500;
const MAX_SUGGESTIONS = 2;

/**
 * 建议问题 (2026-10-05): the AI writes 0–2 questions for this moment — market, contract, time,
 * what the user did and the conversation — instead of fixed sentences. Asked only while the
 * 对话面板 is visible (`enabled`; the caller leaves it off for guests, a model that is not ready,
 * a chat the tier blocks and replay) at three moments: the panel opens (or the conversation's
 * scope changes — Runtime, session, trading account or the signed-in person), a new answer of
 * the current session completes (`lastAnswerId` moves after a turn was in progress), and the
 * contract or view changes (debounced; the old ones go first). A scope change retires the old
 * suggestions, any request still in flight and a pending debounce at once. The newest answer
 * replaces the last; any failure, a 404 from a Runtime without the route included, simply
 * means no suggestions.
 */
export function useChatSuggestions({ baseUrl, enabled, sessionId, accountId, symbol, view, busy, lastAnswerId }: {
  baseUrl: string | undefined;
  enabled: boolean;
  sessionId?: string;
  /** The trading account the conversation acts for: switching it retires the old suggestions. */
  accountId?: string;
  symbol?: string;
  view: ChatSuggestionView;
  /** A turn of the current session is being sent or answered. */
  busy: boolean;
  /** The current session's latest completed answer. */
  lastAnswerId?: string;
}): string[] {
  const [items, setItems] = useState<string[]>([]);
  const identity = useReadIdentity();
  // Only the newest request may land; clearing bumps it too so a late answer cannot come back.
  const sequence = useRef(0);
  const debounce = useRef<number | undefined>(undefined);
  const latest = useRef({ baseUrl, sessionId, symbol, view });
  latest.current = { baseUrl, sessionId, symbol, view };
  const clear = useCallback(() => {
    sequence.current += 1;
    window.clearTimeout(debounce.current);
    debounce.current = undefined;
    setItems([]);
  }, []);
  const ask = useCallback((trigger: ChatSuggestionRequest["trigger"]) => {
    const { baseUrl: url, sessionId: session, symbol: contract, view: place } = latest.current;
    if (!url) return;
    const id = ++sequence.current;
    fetchChatSuggestions(url, { ...(session ? { sessionId: session } : {}), ...(contract ? { symbol: contract } : {}), view: place, trigger })
      .then(result => Array.isArray(result?.items)
        ? result.items.filter((text): text is string => typeof text === "string" && Boolean(text.trim())).slice(0, MAX_SUGGESTIONS)
        : [])
      .catch(() => [] as string[])
      .then(next => { if (id === sequence.current) setItems(next); });
  }, []);

  // The answer that counts as "new": one that completes after a turn was seen in progress in
  // this scope. Loading a session's history moves `lastAnswerId` too, without being a new answer.
  const turnSeen = useRef(false);
  const answered = useRef(false);
  const lastAnswer = useRef(lastAnswerId);

  // The panel opens, or the conversation's scope changes: retire everything of the old scope.
  useEffect(() => {
    clear();
    turnSeen.current = false;
    answered.current = false;
    if (enabled && baseUrl) ask("open");
    return clear;
  }, [enabled, baseUrl, sessionId, accountId, identity, ask, clear]);

  // The contract or view changed while the panel is open: drop the old ones now, ask once it settles.
  const place = `${symbol ?? ""}|${view}`;
  const lastPlace = useRef(place);
  useEffect(() => {
    if (lastPlace.current === place) return;
    lastPlace.current = place;
    if (!enabled) return;
    clear();
    debounce.current = window.setTimeout(() => { debounce.current = undefined; ask("symbol"); }, SUGGESTION_DEBOUNCE_MS);
  }, [place, enabled, ask, clear]);

  // A new answer of the current session completed: asked once nothing is in progress any more.
  // An accepted send (busy back to false before any answer), a failed or cancelled send asks nothing.
  useEffect(() => {
    if (busy) turnSeen.current = true;
    if (lastAnswerId !== lastAnswer.current) {
      lastAnswer.current = lastAnswerId;
      if (lastAnswerId && turnSeen.current) answered.current = true;
    }
    if (answered.current && !busy && enabled) {
      answered.current = false;
      turnSeen.current = false;
      ask("turn");
    }
  }, [busy, lastAnswerId, enabled, ask]);

  return items;
}

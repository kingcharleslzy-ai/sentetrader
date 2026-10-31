import { useEffect, useState } from "react";
import { requestJson } from "../api";
import { LONG_READ_TIMEOUT_MS } from "../requestTimeout";

export type RoundTrip = { id: string; symbol: string; direction: string; volume: number; openTime: number; closeTime: number; openPrice: number; closePrice: number; gross: number; fees: number | null; net: number | null;
  /** A leg synthesized from the order link (limit price, no fill report); older Runtimes omit it. */
  approximate?: boolean; tradingDay?: string;
  /** Who the round trip belongs to (成交 · 来源): a stop-loss close is stop, otherwise the side that
   *  opened it; strategy carries the strategy's name; liquidation = 本站模拟盘强平 (#1204). Older Runtimes and replays omit it. */
  source?: "manual" | "card" | "ai" | "strategy" | "stop" | "liquidation"; strategyName?: string;
  /** 打法本 (2026-10-25): the setup the opening order was placed under (the ticket's 打法); older Runtimes omit it. */
  playbookId?: string; playbookName?: string;
  /** 交易日志 (2026-09-29; older Runtimes omit these): the AI card's or AI order's reason, how long it
   *  was held, the user's 补充, and what the research desk said about the product when it was opened. */
  reason?: string; holdingMinutes?: number; note?: TradeNote | null; deskView?: DeskView | null;
  /** 今日复盘 (2026-10-06; older Runtimes omit them): the 开 and 平 legs' tags 计划内 / 提醒后 / 临时. */
  openTag?: "plan" | "remind" | "adhoc"; closeTag?: "plan" | "remind" | "adhoc" };
export type TradeNote = { text: string; via: "typed" | "voice" | "chat"; updatedAt: string };
/** A 补充 still keyed on a backfilled fill that a real report replaced (#514): no round trip carries it. */
export type UnlinkedNote = TradeNote & { roundId: string };
export type DeskView = { stance: string | null; confidence: string | null; headline: string | null; generatedAt: string | null; researchDay: string | null };
export type TradeReviewData = {
  trades: RoundTrip[];
  account: { equity: number; todayNetPnl: number; commission: number; positionCount: number };
  costSource: string;
  unmatched: unknown[];
  skippedSymbols?: string[];
  /** Older Runtimes omit it. */
  unlinkedNotes?: UnlinkedNote[];
};

/**
 * Round trips from `/api/trade-review`: one contract when `symbol` is given, the whole
 * account otherwise. Polls every 5 s while `enabled`; the review view and the positions
 * bar share this so neither keeps its own copy of the pairing rules.
 */
export function useTradeReview({ baseUrl, symbol, accountId, enabled = true }: { baseUrl: string | undefined; symbol?: string; accountId?: string | null; enabled?: boolean }) {
  const [result, setResult] = useState<{ baseUrl: string; symbol?: string; accountId?: string | null; review?: TradeReviewData; error: string }>();
  useEffect(() => {
    setResult(undefined);
    // null waits for the live account; omitted keeps existing replay/single-account reads.
    if (!baseUrl || !enabled || accountId === null) return;
    let cancelled = false; let timer: ReturnType<typeof setTimeout>;
    const query = symbol ? `?symbol=${encodeURIComponent(symbol)}` : "";
    const poll = async () => {
      try {
        const next = await requestJson<TradeReviewData>(`${baseUrl}/api/trade-review${query}`, {
          headers: accountId === undefined ? undefined : { "X-AI-Trader-Account": accountId }, timeoutMs: LONG_READ_TIMEOUT_MS
        });
        if (!cancelled) setResult({ baseUrl, symbol, accountId, review: next, error: "" });
      }
      catch { if (!cancelled) setResult(previous => ({ baseUrl, symbol, accountId, review: previous?.review, error: "成交记录暂不可用" })); }
      finally { if (!cancelled) timer = setTimeout(poll, 5000); }
    };
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [baseUrl, symbol, accountId, enabled]);
  const current = enabled && accountId !== null && result?.baseUrl === baseUrl
    && result?.symbol === symbol && result?.accountId === accountId ? result : undefined;
  return { review: current?.review, error: current?.error ?? "" };
}

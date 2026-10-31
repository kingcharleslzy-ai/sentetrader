import { useCallback, useEffect, useState } from "react";
import { requestJson } from "./api";

/** 今日复盘 (2026-10-06): one trading day's review, as GET /api/account/daily-review returns it. */
export type ReviewTag = "plan" | "remind" | "adhoc";
export type ReviewLeg = {
  id: string; orderId: string; tradeIds: string[]; symbol: string; name: string; time: string; clock: string;
  side: string; volume: number; price: number; action: string; source: string; sourceLabel: string;
  tag: ReviewTag; tagLabel: string; reason: string; reminder: { kind: string; text: string; clock: string } | null;
  pnl: number | null; pnlBasis: "net" | "gross" | null; approximate: boolean;
  /** 交易验尸 (2026-10-25): the program's checks against the user's plan, the round trip the 笔 belongs to
   *  and the one question answered into its 补充; absent on reviews made before. */
  checks?: { code: string; label: string; text: string }[];
  roundId?: string | null; question?: string | null; playbookName?: string | null;
  entryFit?: { fits: boolean; basis: "strategy" | "model"; text: string };
};
export type ReviewObservation = { text: string; legs: string[]; tradeIds: string[]; key: string;
  /** This version's revision (#524): a 记住 names it; reviews written before it fall back to the key. */
  revision?: string;
  /** 本周最该改的一件事 (2026-10-25): the week's most frequent check and how many 笔 had it. */
  kind?: "weekly"; code?: string; label?: string; count?: number;
  status: "pending" | "remembered" | "dismissed"; memoryId?: string };
export type DailyReview = {
  version: number; tradingDay: string; accountId: string; generatedAt: string; reason: "auto" | "manual";
  stats: { legs: number; planned: number; remind: number; adhoc: number; closes: number; wins: number };
  dayPnl: number | null; commission: number | null; legs: ReviewLeg[];
  /** 当日盈亏的口径 (2026-10-22): settlement = 按结算价（下一交易日的静态权益 − 当天的）; live = 结算前，当天最后一次读数，
   *  结算后复盘自动更正。Absent on reviews made before the field. */
  dayPnlBasis?: "settlement" | "live";
  summary: { text: string; source: "model" | "template"; basis: string };
  observation: ReviewObservation | null; rules: string[];
};
export type DailyReviewResponse = { review: DailyReview | null; days: string[]; generatedAt: string | null; generateAt: string; rules: string[] };

export const TAG_LABELS: Record<ReviewTag, string> = { plan: "计划内", remind: "提醒后", adhoc: "临时" };

const POLL_MS = 60_000;

/** The Runtime's own reason (`detail`, a line or {message}) when it gave one. */
function errorText(error: unknown, fallback: string): string {
  const detail = ((error as { detail?: { detail?: unknown } } | undefined)?.detail)?.detail;
  if (typeof detail === "string" && detail) return detail;
  const message = (detail as { message?: unknown } | undefined)?.message;
  return typeof message === "string" && message ? message : fallback;
}

function errorCode(error: unknown): string | undefined {
  const code = (((error as { detail?: { detail?: unknown } } | undefined)?.detail)?.detail as { code?: unknown } | undefined)?.code;
  return typeof code === "string" ? code : undefined;
}

/**
 * The account's 今日复盘: the latest review (or the day picked), re-read every minute so the 15:15 one
 * appears without a reload; 重新生成 and 本周一件事's 记住 go through the same routes the AI uses.
 */
export function useDailyReview({ baseUrl, accountId, enabled = true }: { baseUrl: string | undefined; accountId?: string | null; enabled?: boolean }) {
  const [day, setDay] = useState<string | null>(null);
  const [data, setData] = useState<{ key: string; value: DailyReviewResponse }>();
  const [busy, setBusy] = useState<"" | "regenerate" | "remember" | "dismiss">("");
  const [notice, setNotice] = useState("");
  const key = `${baseUrl}|${accountId ?? ""}`;
  const headers = accountId ? { "X-AI-Trader-Account": accountId } : undefined;
  useEffect(() => { setDay(null); setNotice(""); }, [key]);
  useEffect(() => {
    if (!baseUrl || !enabled || accountId === null) return;
    let cancelled = false; let timer: ReturnType<typeof setTimeout>;
    const query = day ? `?tradingDay=${encodeURIComponent(day)}` : "";
    const read = async () => {
      try {
        const value = await requestJson<DailyReviewResponse>(`${baseUrl}/api/account/daily-review${query}`, { headers });
        // An older Runtime answers something else (or nothing): no block, no snapshot field.
        if (!cancelled && value && "review" in value && Array.isArray(value.days)) setData({ key, value });
      } catch { /* an older Runtime or a passing failure: the block simply stays as it was */ }
      finally { if (!cancelled) timer = setTimeout(read, POLL_MS); }
    };
    void read();
    return () => { cancelled = true; clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, day, enabled]);
  const current = data?.key === key ? data.value : undefined;
  const regenerate = useCallback(async () => {
    if (!baseUrl || !current?.review) return;
    setBusy("regenerate"); setNotice("");
    try {
      const value = await requestJson<DailyReviewResponse>(`${baseUrl}/api/account/daily-review/regenerate`, {
        method: "POST", headers, body: JSON.stringify({ tradingDay: current.review.tradingDay }) });
      setData({ key, value });
      setNotice("已重新生成");
    } catch (error) { setNotice(errorText(error, "没有重新生成，请稍后再试")); }
    finally { setBusy(""); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseUrl, key, current?.review?.tradingDay]);
  const answer = useCallback(async (verdict: "remember" | "dismiss") => {
    if (!baseUrl || !current?.review?.observation) return;
    const review = current.review;
    const shown = current.review.observation;
    setBusy(verdict); setNotice("");
    try {
      // Names the version on screen: one regenerated meanwhile (another tab) is refused, never answered (#524).
      const result = await requestJson<{ observation: ReviewObservation }>(`${baseUrl}/api/account/daily-review/observation`, {
        method: "POST", headers, body: JSON.stringify({ tradingDay: review.tradingDay, verdict,
          observationKey: shown.key, revision: shown.revision ?? shown.key }) });
      setData({ key, value: { ...current, review: { ...review, observation: result.observation } } });
    } catch (error) {
      setNotice(errorText(error, verdict === "remember" ? "没有记住，请重试" : "没有记下，请重试"));
      if (errorCode(error) === "observation_stale") {
        try {
          const value = await requestJson<DailyReviewResponse>(
            `${baseUrl}/api/account/daily-review?tradingDay=${encodeURIComponent(review.tradingDay)}`, { headers });
          if (value && "review" in value && Array.isArray(value.days)) setData({ key, value });
        } catch { /* the minute poll brings the current one */ }
      }
    }
    finally { setBusy(""); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseUrl, key, current]);
  return { data: current, day, setDay, regenerate, answer, busy, notice };
}

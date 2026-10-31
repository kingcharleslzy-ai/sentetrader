import type { ExecutionCard, JudgmentLine, ResearchJudgmentCard, StrategyBacktestCard } from "@ai-trader/ui-events";
import type { ConversationCard, ConversationTurn } from "../../state/conversation";
import type { DeskReport } from "../ResearchDesk";
import type { FlowItem } from "../futures/aiFlow";
import { productKey } from "./notifyItems";

/** The cards that wait for the user's 确认: a trade, a close batch or a spread. */
export type ProposalCard = Extract<ConversationCard, { type: "trade" | "close-batch" | "spread" }>;

/**
 * 手机版 AI 首页 (2026-10-04; 新结构 2026-10-06): the AI's messages to the user as one stream, newest
 * first — the research desk's judgment of each read product, the conversation's 执行回报 / 确认卡 /
 * 回测 cards and the briefs. Everything comes from what the window already reads (the session's
 * events, the briefs query, the desk reports); this only gathers it. 提醒 left the stream on
 * 2026-10-15: they are the Runtime's record now (最新提醒 and the 提醒 page, `reminders.ts`).
 */
export type MobileFeedItem =
  | { kind: "judgment"; id: string; time: number; card: ResearchJudgmentCard }
  | { kind: "execution"; id: string; time: number; card: ExecutionCard }
  | { kind: "proposal"; id: string; time: number; card: ProposalCard }
  | { kind: "backtest"; id: string; time: number; card: StrategyBacktestCard }
  | { kind: "brief"; id: string; time: number; item: FlowItem };

const FEED_LIMIT = 50;

/** A card's 确认 stays open this long after it is issued (the Runtime's approval_expires_at =
 *  utc_after(600) in proposals.py); after that the card can no longer be confirmed. */
export const APPROVAL_WINDOW_MS = 600_000;

/** A card still waiting for 确认 at `now`: its status asks for approval and its window is open. */
export function isPending(item: MobileFeedItem, now: number): item is Extract<MobileFeedItem, { kind: "proposal" }> {
  return item.kind === "proposal" && item.card.payload.status === "approval_required" && now - item.time < APPROVAL_WINDOW_MS;
}

/** Whole minutes left to confirm a pending card (at least 1 while it is pending). */
export function approvalMinutesLeft(item: MobileFeedItem, now: number): number {
  return Math.min(APPROVAL_WINDOW_MS / 60_000, Math.max(1, Math.ceil((item.time + APPROVAL_WINDOW_MS - now) / 60_000)));
}

const at = (value: string | null | undefined, fallback: number) => {
  const time = value ? Date.parse(value) : Number.NaN;
  return Number.isFinite(time) ? time : fallback;
};

export function buildMobileFeed({ turns, briefs, desks = [] }: { turns: ConversationTurn[]; briefs: FlowItem[]; desks?: readonly (ResearchJudgmentCard | null)[] }): MobileFeedItem[] {
  // A card republished under the same id (生成中 → 完成, a later fill) keeps only its latest form.
  const cards = new Map<string, MobileFeedItem>();
  turns.forEach((turn, index) => {
    if (turn.hidden) return;
    const turnTime = at(turn.timestamp, index);
    for (const card of turn.cards) {
      if (card.type === "research-judgment") cards.set(card.id, { kind: "judgment", id: card.id, time: at(card.payload.generatedAt, turnTime), card: card.payload });
      else if (card.type === "execution") cards.set(card.id, { kind: "execution", id: card.id, time: at(card.payload.updatedAt, turnTime), card: card.payload });
      // A 确认卡 keeps the time it was issued: confirmed, rejected or expired, it stays where it came in.
      else if (card.type === "trade" || card.type === "close-batch" || card.type === "spread") {
        cards.set(card.id, { kind: "proposal", id: card.id, time: cards.get(card.id)?.time ?? at(card.payload.updatedAt, turnTime), card });
      } else if (card.type === "strategy-backtest") {
        cards.set(card.id, { kind: "backtest", id: card.id, time: at(card.payload.createdAt, turnTime), card: card.payload });
      }
    }
  });
  // One judgment per product, research day and state: the conversation may carry the same day's
  // judgment on several cards (asked twice, the morning card) and the desk reads it again — by
  // product, not by contract spelling, the conversation's newest card stays (a row opens it).
  const judgmentKey = (card: ResearchJudgmentCard) => `${productKey(card.symbol || "").toLowerCase()}|${card.researchDay ?? ""}|${card.status}`;
  const judgments = new Map<string, Extract<MobileFeedItem, { kind: "judgment" }>>();
  const items = [...cards.values()].filter(item => {
    if (item.kind !== "judgment") return true;
    const key = judgmentKey(item.card);
    const kept = judgments.get(key);
    if (!kept || item.time >= kept.time) judgments.set(key, item);
    return false;
  });
  // A desk judgment joins unless the conversation (or another desk read) already carries the same or
  // a newer version of it: a same-day judgment generated later (the desk refreshed after the chat's
  // card) replaces the older card, while the same version keeps the conversation's card (a row
  // opens it) and an older desk read never covers a newer card.
  for (const desk of desks) {
    if (!desk) continue;
    const key = judgmentKey(desk);
    const kept = judgments.get(key);
    const version = at(desk.generatedAt, Number.NaN);
    if (kept && !(version > kept.time)) continue;
    // The row id names the version (research day and generation time): a read mark on yesterday's or an earlier
    // version of today's judgment does not mark a newer one read (#516).
    judgments.set(key, { kind: "judgment", id: `${desk.cardId}@${desk.researchDay ?? ""}@${desk.generatedAt ?? desk.status}`,
      time: Number.isNaN(version) ? Date.now() : version, card: desk });
  }
  items.push(...judgments.values());
  items.push(...briefs.map(item => ({ kind: "brief" as const, id: item.id, time: item.time, item })));
  return items.sort((a, b) => b.time - a.time).slice(0, FEED_LIMIT);
}

const DESK_NOTES: Record<string, string> = {
  disabled: "当前档位不能生成调研判断。",
  insufficient: "这个品种的调研数据还不够，暂不出判断。",
  failed: "今日判断生成失败，稍后会重试。",
  budget_exhausted: "今天的判断额度已用完，明天 08:30 翻篇后再生成。",
  signin_required: "登录后可看 AI 判断。",
};

/** The desk report's judgment as a 判断卡 (the same mapping the Runtime uses for the chat's card). */
export function deskJudgmentCard(report: DeskReport): ResearchJudgmentCard {
  const judgment = report.judgment;
  const productName = report.productName || report.symbol;
  const card: ResearchJudgmentCard = {
    turnId: "mobile-feed", cardId: `desk:${report.mainSymbol || report.symbol}`, symbol: report.mainSymbol || report.symbol,
    productName, status: "pending", facts: [], invalidation: [],
    actions: [{ kind: "ask", label: "问 AI 怎么看", prompt: `${productName}今天怎么看？` }],
  };
  if (judgment.status === "pending" || (judgment.status === "ready" && judgment.refreshing && judgment.today === false)) {
    return { ...card, note: "今日判断生成中，完成后会在这里更新。" };
  }
  if (judgment.status !== "ready") return { ...card, status: "unavailable", note: DESK_NOTES[judgment.status] ?? "暂时拿不到调研判断。" };
  // A cited fact keeps the research desk's own number ([3] = the third fact on 调研台), so the line
  // can be checked against the page that lists the facts.
  const numbers = new Map((report.facts ?? []).map((fact, index) => [fact.id, index + 1]));
  const lines = (points: NonNullable<typeof judgment.points> | undefined, limit: number): JudgmentLine[] => (points ?? []).slice(0, limit)
    .filter(point => point.text)
    .map(point => {
      const id = point.basis?.[0] ?? "";
      const cited = judgment.basis?.[id];
      const number = numbers.get(id);
      return { text: point.text, quote: point.quote, source: [number ? `[${number}]` : "", cited?.source ?? ""].filter(Boolean).join(" ") || undefined,
        asOf: cited?.asOf ?? null };
    });
  const sources = report.facts?.length ?? 0;
  const stance = ["偏多", "偏空", "震荡", "不明"].includes(judgment.stance ?? "") ? judgment.stance as ResearchJudgmentCard["stance"] : "不明";
  const confidence = ["低", "中", "高"].includes(judgment.confidence ?? "") ? judgment.confidence as ResearchJudgmentCard["confidence"] : null;
  return { ...card, status: "ready", stance, confidence, headline: judgment.headline ?? null,
    facts: lines(judgment.points, 4), invalidation: lines(judgment.risks, 3), generatedAt: judgment.generatedAt ?? null,
    researchDay: judgment.researchDay ?? null,
    note: judgment.today === false ? `这是 ${judgment.researchDay} 的判断，今日的还没生成。` : sources ? `来源 ${sources} 项` : null };
}

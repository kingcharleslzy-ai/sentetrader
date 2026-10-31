import { useSyncExternalStore } from "react";
import { requestJson } from "./api";
import type { DeskFact } from "./components/ResearchDesk";

/**
 * 多 AI 研讨 (2026-10-11): the Runtime's debate routes (/api/research/debates) and a tiny shared store —
 * whether the 辩论 page is open (its own completion then needs no toast) and a version
 * that moves whenever a debate starts or ends (the 快讯 rows re-read their 已研讨 tags).
 */
export type DebateSide = "bull" | "bear";
export type DebateArgument = { id: string; side: DebateSide; text: string; factIds: string[]; quote: string; verified: boolean;
  /** Why the program's check failed (未核对: the judge never saw it). */
  problem?: string;
  /** Round two: the other side's argument it answers, and whether it concedes it. */
  target?: string; concede?: boolean };
export type DebateFact = DeskFact & { n: number; sheet: string;
  /** A web page an analyst found for a gap in its area (section "web", 2026-10-24). */
  found?: { analyst: AnalystKey; gap: string } };
export type AnalystKey = "technical" | "supply" | "flow" | "news";
/** A checked line of a report: its conclusion or one key point (a key point carries its label). */
export type ReportLine = { text: string; factIds: string[]; verified: boolean; problem?: string; label?: string };
/** One analyst's report (2026-10-24): its text so far while it writes, its checked parts after; 没写成 = failed. */
export type DebateReport = { key: AnalystKey; title: string; status: "wait" | "run" | "done" | "failed"; text: string;
  conclusion: ReportLine | null; sentences: DebateArgument[]; points: ReportLine[];
  /** Fact ids of the web pages it found (the 全网补充 chip), and each search it ran. */
  found: string[]; searches: { gap: string; query: string; found: number; error?: string }[];
  gaps: { label: string; state: "missing" | "stale" }[]; model?: string; seconds?: number; error?: string };
export type DebateSheet = { key: "position" | "basis" | "term" | "news" | "technical"; title: string;
  status: "complete" | "partial" | "missing" | "stale"; factIds: string[]; missing: string[] };
/** The run's steps: collect, the analysts (2026-10-24), the four turns, the judge (round1 / round2: a record from before
 *  the turns). */
export type DebateStep = { key: "collect" | "analysts" | "turn1" | "turn2" | "turn3" | "turn4" | "judge" | "round1" | "round2";
  status: "wait" | "run" | "done" | "skipped" | "failed"; seconds?: number; startedAt?: string; side?: DebateSide;
  detail?: { facts?: number; event?: boolean; done?: number; failed?: number; searches?: number } };
/** One spoken turn (2026-10-24): its text so far while it runs, its checked sentences once it ends. A sentence's
 *  text carries the model's inline markers ([F12] = 资料来源 number 12); an older record's arguments carry none. */
export type DebateTurn = { n: number; side: DebateSide; status: "run" | "done" | "failed"; text: string; sentences: DebateArgument[];
  model?: string; seconds?: number; startedAt?: string };
export type DebatePick = { id: string; side: DebateSide; reason: string };
export type DebateStance = "偏多" | "略偏多" | "震荡" | "略偏空" | "偏空" | "看不清";
export type DebateVerdict = { stance: DebateStance; winner: DebateSide | null; margin: "明显" | "略" | null; insufficient: boolean; headline: string;
  /** The judge's plain-language why (absent before 2026-10-24). */
  reason?: string;
  adopted: DebatePick[]; rejected: DebatePick[]; swing: string; changeMind: string; invalidation: number | null; watch: string[];
  /** The key disagreements and 如果…那么… scenarios (absent before 2026-10-24). */
  disagreements?: string[]; scenarios?: string[];
  confidence: "低" | "中" | "高";
  confidenceBasis: { evidenceShare: number; refuted: number; missingSheets: number; staleSheets: number; cap: string; score: number } };
// fallbackFrom/fallbackReason: this call's model stood in for the role's own one, which was unavailable.
export type DebateCall = { role: "bull" | "bear" | "judge" | "analyst"; stage: string; model: string; tokens: number; seconds: number; error?: string; output?: unknown;
  fallbackFrom?: string; fallbackReason?: string };
export type DebateSettlement = { tradingDay: string; due: string; entryPrice: number | null;
  /** quote: a current quote; last: the market was shut after the current trading day traded, its last price;
   * close: the market was shut before it traded, the last close stood in (its day is tradingDay). */
  entrySource?: "quote" | "last" | "close" | null;
  /** No 今日判断 of this research day when it began: the single model is not scored this time. */
  deskMissing?: boolean; invalidation: number | null; stance: string | null;
  deskStance: string | null; leans: Partial<Record<DebateSide, string>>; status: "pending" | "settled" | "void"; closes: number[];
  outcomes: Partial<Record<"judge" | "bull" | "bear" | "desk", "right" | "wrong">>; reason?: string; settledAt?: string };
export type DebateTopic = { kind: "event" | "today"; eventId?: string; title: string; text?: string; publishedAt?: string | null; tier?: string;
  direction?: string; strength?: string; transmission?: string; reports?: number; question: string };
export type Debate = {
  id: string; status: "queued" | "running" | "done" | "review" | "failed" | "cancelled"; symbol: string; productKey: string; name: string;
  researchDay: string; createdAt: string; finishedAt: string | null; seconds: number | null; topic: DebateTopic;
  sheets: DebateSheet[]; facts: DebateFact[]; price: number | null; steps: DebateStep[];
  /** The exchange, turn by turn (the Runtime reads an older record's two rounds as turns too). */
  turns: DebateTurn[];
  leans: Partial<Record<DebateSide, string>>; verdict: DebateVerdict | null; review: boolean; error: string | null;
  models: Record<"bull" | "bear" | "judge", string> & { analyst?: string | null };
  /** The four analysts' reports and 上次复盘 (absent before 2026-10-24); `searches` = web searches this run made. */
  analysts?: DebateReport[]; lesson?: { id: string; researchDay: string; outcome: "right" | "wrong"; text: string } | null; searches?: number;
  // A role's model handed over to its fallback (models holds the one that spoke last).
  fallbacks?: { role: "bull" | "bear" | "judge"; stage: string; from: string; to: string; reason: string }[]; mapping: { A: DebateSide; B: DebateSide } | null; calls: DebateCall[]; tokens: number;
  settlement: DebateSettlement | null; adopted: { at: string; stance: string } | null;
  compare: { stance?: string; confidence?: string; headline?: string; generatedAt?: string } | null;
  /** Of the research day now, by the Runtime's clock (only today's may be written into 今日判断). */
  today?: boolean;
  /** 需复核 because no adopted argument with its own evidence backs the verdict: the judge's reading, never shown as a tier. */
  unsupportedVerdict?: DebateVerdict | null;
  /** The exchange's counts as the Runtime reckons them (the chat AI reads the same numbers). */
  checks?: { rounds: number; arguments: number; unverified: number; cited: number };
  previousId?: string; reused?: boolean };
export type DebateSummary = Pick<Debate, "id" | "status" | "symbol" | "productKey" | "name" | "researchDay" | "createdAt" | "finishedAt"> & {
  topic: { kind: "event" | "today"; eventId?: string | null; title?: string }; stance: DebateStance | null; confidence: string | null;
  /** 以前的辩论 (2026-10-24): who won, and the 5-day reckoning of the judge's call (absent from an older Runtime). */
  winner?: DebateSide | null; margin?: "明显" | "略" | null; insufficient?: boolean | null;
  settlement?: { status: "pending" | "settled" | "void"; due?: string; outcome?: "right" | "wrong" | null } | null };
export type DebateStats = { settled: number; pending: number; minSamples: number;
  rates: Record<"bull" | "bear" | "judge" | "desk", { right: number; total: number }> | null };
export type DebateList = { items: DebateSummary[]; stats: DebateStats; day: string };

export const DEBATE_RUNNING = new Set<Debate["status"]>(["queued", "running"]);
const MODEL_NAMES: Record<string, string> = { "deepseek-flash": "DeepSeek", "deepseek-v4-pro": "DeepSeek", "glm-5.3-flash": "GLM", "gpt-6-astra": "GPT-6 Astra", "kimi-k3": "Kimi K3", "k3.1-pro": "Kimi K3.1 Pro" };
export const modelName = (slug: string | undefined) => (slug && MODEL_NAMES[slug]) || slug || "—";
const FALLBACK_REASONS: Record<string, string> = { model_rate_limited: "限流", model_auth_failed: "认证或额度失败", TimeoutError: "超时",
  model_request_failed: "请求失败", model_stream_interrupted: "连接中断", model_unavailable: "不可用", model_not_configured: "未配置密钥" };
export const fallbackReason = (code: string | undefined) => (code && FALLBACK_REASONS[code]) || code || "不可用";
/** Who spoke for a role: its model, or "GLM→Kimi K3" when the replaced model had already answered before the switch. */
export const roleModels = (debate: Pick<Debate, "models" | "calls" | "fallbacks">, role: "bull" | "bear" | "judge") => {
  const switches = (debate.fallbacks ?? []).filter(item => item.role === role);
  const spoke = switches.filter(item => debate.calls.some(call => call.role === role && call.model === item.from && !call.error)).map(item => item.from);
  return [...spoke, debate.models[role]].map(modelName).join("→");
};

/** The Runtime's refusal in words: `detail.message` from the debate routes, else the status. */
export function debateError(cause: unknown): string {
  const body = (cause as { detail?: { detail?: unknown } }).detail?.detail;
  if (body && typeof body === "object" && typeof (body as { message?: unknown }).message === "string") return (body as { message: string }).message;
  const status = (cause as { status?: number }).status;
  return status ? `HTTP ${status}` : "网络错误";
}

/** An event needs only its id: the Runtime takes its product from the event (`symbol` only picks among them). */
export const startDebate = (baseUrl: string, body: { symbol?: string; eventId?: string; force?: boolean }) =>
  requestJson<Debate>(`${baseUrl}/api/research/debates`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export const getDebate = (baseUrl: string, id: string) => requestJson<Debate>(`${baseUrl}/api/research/debates/${encodeURIComponent(id)}`);
export const listDebates = (baseUrl: string, query: { product?: string; events?: string[]; day?: string; limit?: number }) => {
  const params = new URLSearchParams();
  if (query.product) params.set("product", query.product);
  if (query.events?.length) params.set("events", query.events.join(","));
  if (query.day) params.set("day", query.day);
  if (query.limit) params.set("limit", String(query.limit));
  return requestJson<DebateList>(`${baseUrl}/api/research/debates?${params}`);
};
export const adoptDebate = (baseUrl: string, id: string) =>
  requestJson<{ debate: Debate; judgment: { stance: string; generatedAt: string } }>(`${baseUrl}/api/research/debates/${encodeURIComponent(id)}/adopt`, { method: "POST" });
export const cancelDebate = (baseUrl: string, id: string) => requestJson<Debate>(`${baseUrl}/api/research/debates/${encodeURIComponent(id)}/cancel`, { method: "POST" });

let viewOpen = false;
/** The debate the open sub-view shows (its own completion needs no toast; another one's still does). */
let shownId: string | undefined;
let version = 0;
const listeners = new Set<() => void>();
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
const emit = () => listeners.forEach(listener => listener());

/** The 辩论 page is showing (the host says so while it renders one). */
export function setDebateViewOpen(open: boolean) {
  if (open === viewOpen) return;
  viewOpen = open;
  emit();
}
/** Non-reactive: is this debate on screen right now (its completion then needs no toast). */
export const debateShownNow = (id: string | undefined) => viewOpen && Boolean(id) && id === shownId;
export function setShownDebate(id: string | undefined) { shownId = id; }

/** The research day the Runtime is in: it turns at 08:30 Beijing, i.e. 00:30 UTC. */
export const researchDayNow = (now = Date.now()) => new Date(now - 30 * 60_000).toISOString().slice(0, 10);
/** A debate started, ended or was written into 今日判断: tags and the desk re-read. */
export function bumpDebates() { version += 1; emit(); }
export function useDebateVersion(): number { return useSyncExternalStore(subscribe, () => version, () => 0); }

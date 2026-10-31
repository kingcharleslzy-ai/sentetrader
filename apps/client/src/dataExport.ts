import type { ConversationTurn } from "./state/conversation";
import { voiceCallStore } from "./voiceRealtime";
import type { TradeReviewData } from "./hooks/useTradeReview";
import { clearCacheDb } from "./localCache";

/** 设置 → 数据与隐私: exports (a browser download) and the local-cache reset. */
export function downloadText(name: string, text: string, type = "application/json") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = document.createElement("a");
  link.href = url; link.download = name; link.rel = "noopener";
  document.body.appendChild(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const stamp = () => new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");

export function conversationJson(turns: ConversationTurn[], sessionId: string | undefined): string {
  return JSON.stringify({ exportedAt: new Date().toISOString(), sessionId: sessionId ?? null, turns }, null, 2);
}

/** Markdown: one heading per turn, cards listed by kind and title. */
export function conversationMarkdown(turns: ConversationTurn[], sessionId: string | undefined): string {
  const lines = [`# Sente 对话${sessionId ? ` · ${sessionId}` : ""}`, "", `导出时间：${new Date().toLocaleString("zh-CN")}`, ""];
  for (const turn of turns) {
    lines.push(`## ${turn.role === "user" ? "我" : "AI"} · ${new Date(turn.timestamp).toLocaleString("zh-CN")}${turn.origin === "voice" ? " · 语音" : ""}`, "");
    if (turn.text) lines.push(turn.text, "");
    for (const card of turn.cards) {
      const record = card as unknown as Record<string, unknown>;
      const title = typeof record.title === "string" ? record.title : "";
      lines.push(`- 卡片 ${String(record.kind ?? record.type ?? "card")}${title ? `：${title}` : ""}`);
    }
    if (turn.cards.length) lines.push("");
  }
  return lines.join("\n");
}

export function exportConversation(turns: ConversationTurn[], sessionId: string | undefined, format: "json" | "markdown") {
  if (format === "json") downloadText(`sente-对话-${stamp()}.json`, conversationJson(turns, sessionId));
  else downloadText(`sente-对话-${stamp()}.md`, conversationMarkdown(turns, sessionId), "text/markdown");
}

const when = (ns: number) => new Date(ns / 1e6).toLocaleString("zh-CN");
/** The export's window: the longest `/api/trade-review` serves (its `days` is 1–366; the route's own default is 30). */
export const REVIEW_EXPORT_DAYS = 366;

/** 导出复盘: round trips closed in the last `days` days, with what the review could not count said in the file
 *  itself — contracts whose parameters are gone, closes without an opening fill, legs priced from the order (#932 F6). */
export function reviewMarkdown(review: TradeReviewData | undefined, days: number): string {
  const trades = review?.trades ?? [];
  const lines = ["# Sente 复盘", "", `导出时间：${new Date().toLocaleString("zh-CN")}`, `时间窗：近 ${days} 天内平仓的记录`, ""];
  if (review) lines.push(`账户权益 ${review.account.equity} · 今日净盈亏 ${review.account.todayNetPnl} · 手续费 ${review.account.commission}（${review.costSource}）`, "");
  const notes: string[] = [];
  // These two come from the account's whole fill history, not just the window (the Runtime does not date them).
  if (review?.skippedSymbols?.length) notes.push(`- 账户成交记录中 ${review.skippedSymbols.join("、")} 的合约参数已不可用，其成交未计入（不限时间窗）。`);
  if (review?.unmatched.length) notes.push(`- 账户成交记录中有 ${review.unmatched.length} 笔平仓缺少对应开仓记录（未配对），未推算其收益（不限时间窗）。`);
  if (trades.some(trade => trade.approximate)) notes.push("- 标“估算”的记录按委托价推算，没有成交回报。");
  if (notes.length) lines.push(...notes, "");
  if (!trades.length) { lines.push(`近 ${days} 天暂无已平仓记录。`); return lines.join("\n"); }
  lines.push("| 合约 | 方向 | 开仓 | 平仓 | 手数 | 毛利 | 净利 |", "| --- | --- | --- | --- | --- | --- | --- |");
  for (const trade of trades) lines.push(`| ${trade.symbol}${trade.approximate ? "（估算）" : ""} | ${trade.direction} | ${trade.openPrice} @ ${when(trade.openTime)} | ${trade.closePrice} @ ${when(trade.closeTime)} | ${trade.volume} | ${trade.gross} | ${trade.net ?? "—"} |`);
  return lines.join("\n");
}

export function exportReview(review: TradeReviewData | undefined, format: "json" | "markdown", days = REVIEW_EXPORT_DAYS) {
  if (format === "json") { downloadText(`sente-复盘-${stamp()}.json`, JSON.stringify({ exportedAt: new Date().toISOString(), days, review: review ?? null }, null, 2)); return; }
  downloadText(`sente-复盘-${stamp()}.md`, reviewMarkdown(review, days), "text/markdown");
}

/**
 * Diagnostics: environment, the signed-in person's last voice call's event timeline (audio frames
 * omitted; empty after a sign-out or another sign-in, #523) and, on the
 * desktop, the tail of the shell's frontend.log. Web: a browser download; desktop: a save-as dialog
 * through the shell. Resolves to the status line for the settings row.
 */
export async function exportDiagnostics(extra: Record<string, unknown>): Promise<string> {
  const desktop = "__TAURI_INTERNALS__" in window;
  const report: Record<string, unknown> = { exportedAt: new Date().toISOString(), userAgent: navigator.userAgent, desktop, ...extra,
    lastVoiceTimeline: voiceCallStore.lastTimeline() };
  const name = `sente-诊断-${stamp()}.json`;
  if (!desktop) { downloadText(name, JSON.stringify(report, null, 2)); return "已导出"; }
  const { invoke } = await import("@tauri-apps/api/core");
  try {
    const log = await invoke<{ path: string; text: string; truncated: boolean }>("desktop_frontend_log", { maxBytes: 200 * 1024 });
    report.frontendLog = { path: log.path, truncated: log.truncated, text: log.text };
  } catch (error) { report.frontendLog = { error: error instanceof Error ? error.message : String(error) }; }
  try {
    const saved = await invoke<string | null>("desktop_save_text_file", { defaultName: name, contents: JSON.stringify(report, null, 2) });
    return saved ? `已保存到 ${saved}` : "已取消";
  } catch (error) { return `保存失败：${error instanceof Error ? error.message : String(error)}`; }
}


const KEEP = new Set(["ai-trader:web-device", "ai-trader:last-username"]);
/** Drops this browser's app caches and remembered layout; identity and the last username stay. Returns the count. */
export function clearLocalCache(): number {
  let removed = 0;
  try {
    for (const key of Object.keys(localStorage)) if (key.startsWith("ai-trader") && !KEEP.has(key)) { localStorage.removeItem(key); removed += 1; }
  } catch { /* Storage unavailable. */ }
  try { void caches?.keys().then(keys => keys.forEach(key => void caches.delete(key))); } catch { /* No cache storage. */ }
  // The charts' K-line bars and the 快讯 rows (IndexedDB, localCache.ts).
  void clearCacheDb();
  return removed;
}

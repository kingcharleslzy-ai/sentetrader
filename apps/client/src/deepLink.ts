/**
 * 锁屏提醒的去处 (2026-10-06): a notification opens the app at `?open=…` (or, when a window is
 * already open, the service worker posts the same address to it). One of:
 *   card:<cardId>          the conversation, scrolled to that card (a reminder, a proposal, an execution)
 *   chat                   the conversation
 *   mobilePage:positions   持仓与委托 (the positions panel on a computer)
 *   research               调研 (the research desk)
 *   research:sheet         调研 · 今日作战表 (the morning push, 2026-10-25)
 *   review:<2026-10-09>    复盘 on that trading day's 今日复盘 (the phone's 复盘摘要); a USD sub-account's 外盘复盘
 *                          adds its account (review:<2026-10-21>:paperusd:<id>) and opens on that account
 *   symbol:<SHFE.rb2610>   that contract's chart
 * The parameter is read once and removed from the address bar.
 */
import { listenToServiceWorker } from "./serviceWorkerPolicy";

export type DeepLink =
  | { kind: "card"; id: string }
  | { kind: "chat" }
  | { kind: "positions" }
  | { kind: "research"; page?: "sheet" }
  | { kind: "review"; day: string; accountId?: string }
  | { kind: "symbol"; symbol: string };

export function parseDeepLink(value: string | null | undefined): DeepLink | null {
  if (!value) return null;
  const [kind, ...rest] = value.split(":");
  const argument = rest.join(":");
  if (kind === "card" && argument && argument.length <= 200) return { kind: "card", id: argument };
  if (kind === "chat") return { kind: "chat" };
  if (kind === "mobilePage" && argument === "positions") return { kind: "positions" };
  if (kind === "research") return argument === "sheet" ? { kind: "research", page: "sheet" } : { kind: "research" };
  const review = /^(\d{4}-\d{2}-\d{2})(?::(paperusd:[A-Za-z0-9]+))?$/.exec(kind === "review" ? argument : "");
  if (review) return review[2] ? { kind: "review", day: review[1], accountId: review[2] } : { kind: "review", day: review[1] };
  // The Runtime's instruments.SYMBOL_PATTERN: a contract, or a 快期主连 / 外盘主连 (KQD.m@CME.NQ, 外盘代码统一 2026-10-21).
  if (kind === "symbol" && /^(?:KQD?\.[A-Za-z]@)?[A-Za-z]+\.[A-Za-z0-9]+$/.test(argument)) return { kind: "symbol", symbol: argument };
  return null;
}

export function deepLinkFromUrl(url: string): DeepLink | null {
  try { return parseDeepLink(new URL(url, window.location.href).searchParams.get("open")); } catch { return null; }
}

/** The link this page was opened with, removed from the address bar so a reload does not repeat it. */
export function takeDeepLink(): DeepLink | null {
  if (typeof window === "undefined") return null;
  const url = new URL(window.location.href);
  const value = url.searchParams.get("open");
  if (value === null) return null;
  url.searchParams.delete("open");
  try { window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash); } catch { /* keep it */ }
  return parseDeepLink(value);
}

/** A notification tapped while this window was already open (the service worker's message). */
export function onDeepLinkMessage(listener: (link: DeepLink) => void): () => void {
  const handle = (event: MessageEvent) => {
    const data = event.data as { type?: string; url?: string } | undefined;
    if (data?.type !== "sente:open" || typeof data.url !== "string") return;
    const link = deepLinkFromUrl(data.url);
    if (link) listener(link);
  };
  return listenToServiceWorker(handle);
}

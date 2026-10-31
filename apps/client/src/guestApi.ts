import type { UiChanges } from "./hooks/useUiControl";
import { withRequestTimeout, type RequestTimeoutOption } from "./requestTimeout";
/** The settings pages a visitor may open (the Runtime's guest whitelist, guest.py, says the same):
 * one list for the settings drawer, the open-settings event and the visitor's UI commands (AI 审计 2026-10-13). */
export const GUEST_SETTINGS_PAGES: readonly string[] = ["chart", "ui", "chart-advanced", "about", "feedback"];
/** Guest credentials are memory-only and never enter the owner's token/account registry. */
const guestTokens = new Map<string, string>();
const normalize = (baseUrl: string) => baseUrl.replace(/\/$/, "");
export function guestAccessTokenFor(baseUrl: string): string | undefined { return guestTokens.get(normalize(baseUrl)); }
export function setGuestAccessToken(baseUrl: string, token?: string) {
  if (token) guestTokens.set(normalize(baseUrl), token);
  else guestTokens.delete(normalize(baseUrl));
}
export function guestHeaders(baseUrl: string, input?: HeadersInit): Headers {
  const headers = new Headers(input);
  headers.delete("Authorization");
  headers.delete("X-AI-Trader-Account");
  const token = guestAccessTokenFor(baseUrl);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return headers;
}
/** Guest traffic never inherits account headers or an owner's bearer token. */
export async function guestRequest<T>(baseUrl: string, path: string, init?: RequestInit & RequestTimeoutOption): Promise<T> {
  const { timeoutMs, ...rest } = init ?? {};
  const headers = guestHeaders(baseUrl, rest.headers);
  headers.set("Content-Type", "application/json");
  // Reads share requestJson's default time limit (#712 B1); writes have none.
  return withRequestTimeout({ method: rest.method, signal: rest.signal, timeoutMs }, async signal => {
    const response = await fetch(`${baseUrl}${path}`, { ...rest, credentials: "include", headers, ...(signal ? { signal } : {}) });
    if (!response.ok) throw Object.assign(new Error(response.status === 401 ? "游客会话已失效，请重新连接" : "游客服务暂时不可用，请重试"), { status: response.status });
    return response.json() as Promise<T>;
  });
}
export interface GuestSession {
  role: "guest"; guestId: string; expiresAt: string; readOnly: true; voiceEnabled: boolean; windowId: string;
  accessToken?: string;
  marketHistoryTradingDays?: number;
}
/** The chart's visible basis for a guest's shortened history (#734). `days` is the limit the market
 *  frame itself carries (`workbench.historyTradingDays`), so an administrator's change shows with the
 *  re-trimmed bars; a frame without it (an older Runtime) gets the sentence without a number. */
export function guestHistoryNote(days?: number | null): string {
  return days && days > 0 ? `游客仅显示最近 ${days} 个交易日 · 登录看完整历史` : "游客仅显示最近几个交易日 · 登录看完整历史";
}
/** Creates a guest session. Its token is installed only while `stillWanted()` holds when the answer
 *  arrives: a page that unmounted or moved on meanwhile never gets a late guest token (#712). */
export async function startGuestSession(baseUrl: string, stillWanted: () => boolean): Promise<GuestSession | undefined> {
  const session = await guestRequest<GuestSession>(baseUrl, "/api/auth/guest", { method: "POST" });
  if (!stillWanted()) return undefined;
  setGuestAccessToken(baseUrl, session.accessToken);
  return session;
}
export interface GuestChanges extends Pick<UiChanges, "panel" | "settingsOpen" | "settingsPage" | "dockSide" | "dockWidth" | "fontScale" | "density" | "chartSizes" | "chartOption" | "pinnedSymbols" | "dialog" | "disclosure" | "presentation" | "mobileTab" | "mobileMarket"> {
  paneSizes?: { main: number; kd: number; macd: number }; researchExpanded?: boolean;
  kd?: boolean; macd?: boolean; volume?: boolean; news?: { tab?: "desk" | "news" | "data"; pinnedSymbol?: string | null };
  symbol?: string; duration?: number; theme?: "light" | "dark" | "system"; newsOpen?: boolean; loginOpen?: boolean;
}

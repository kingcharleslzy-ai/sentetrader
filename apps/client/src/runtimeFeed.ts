import { useSyncExternalStore } from "react";
import type { UiEvent } from "@ai-trader/ui-events";
import type { AccountOverviewSnapshot, AiExecutionSnapshot, MarketBoardRow, MarketBoardSnapshot, MarketWorkbenchSnapshot } from "./api";
import type { ActivitySnapshot } from "./runtimeClient";
import { mergeGuestMarket, type GuestMarketFrame } from "./guestMarket";
import { feedMarketFrame, keepFeedFocusOf } from "./marketCache";
import { rumPriceFrame } from "./rum";

/**
 * Snapshots the Runtime pushes over the session's SSE stream. Polling stays
 * only as a fallback: a reader is "live" while *its own* channel arrived within
 * FEED_STALE_MS and the Runtime reported that channel usable, otherwise that
 * one poller resumes its old interval. One shared "last event" timestamp used
 * to let a ticking execution-control channel hide a market that stopped
 * updating, so each channel now carries its own receipt and status.
 *
 * The market channel is pushed on change only (the shared market source; the
 * HTTP read serves the same packet). A closed or quiet market is silent, not
 * stale: while the session's stream is alive (its heartbeat arrives every
 * second when idle) an ok market channel stays live, and nothing polls it.
 * The Runtime reports a failed market read on feed.status as before.
 */
export type FeedChannel = "account" | "market" | "activity" | "execution" | "board";

export const FEED_CHANNELS: readonly FeedChannel[] = ["account", "market", "activity", "execution", "board"];

export interface FeedChannelState {
  /** When this client received the last event on the channel. */
  receivedAt: number;
  /** The Runtime's own capture time for the payload, when it reports one. */
  sourceAt?: string;
  /** "unavailable" is pushed by the Runtime when its read for this channel failed. */
  status: "ok" | "unavailable";
  /** Why the Runtime cannot serve the channel (only with status "unavailable"). */
  error?: string;
}

/** The shared market's version of a pushed market (feed.market `revision`), the base for patches. */
export type FeedMarketSnapshot = MarketWorkbenchSnapshot & { revision?: string };

/** The pushed 报价总表 (2026-10-12): the rows this window asked for (`key`, see feedView.ts) at board
 *  revision `feedRevision`, the base of the next feed.board-patch. The server refreshes it at least
 *  every 5 s, so an ordinary FEED_STALE_MS silence means the push stopped and the poll takes over. */
export type FeedBoardSnapshot = MarketBoardSnapshot & { feedRevision: string; key: string };

export interface RuntimeFeedState {
  account?: AccountOverviewSnapshot;
  market?: FeedMarketSnapshot;
  board?: FeedBoardSnapshot;
  activity?: ActivitySnapshot;
  execution?: AiExecutionSnapshot;
  channels: Partial<Record<FeedChannel, FeedChannelState>>;
  /** The session these snapshots belong to (for the stream-alive check). */
  sessionId?: string;
}

export const FEED_STALE_MS = 10_000;
/** A stream that sent nothing (not even its idle heartbeat) for this long is not alive. */
export const STREAM_ALIVE_MS = 5_000;
const streamAliveAt = new Map<string, number>();
const EMPTY: RuntimeFeedState = { channels: {} };
const CHANNEL_OF: Record<string, FeedChannel | undefined> = {
  "feed.account": "account", "feed.market": "market",
  "feed.activity": "activity", "feed.execution": "execution", "feed.board": "board"
};
const states = new Map<string, RuntimeFeedState>();
let activeSessionId: string | undefined;
const listeners = new Set<() => void>();
const nudgeListeners = new Set<(reason: string) => void>();
const resyncListeners = new Set<(sessionId: string) => void>();
const boardResyncListeners = new Set<(sessionId: string) => void>();
const signalListeners = new Set<(payload: StrategySignalEvent) => void>();
const priceAlertListeners = new Set<(payload: PriceAlertEvent) => void>();
const reminderListeners = new Set<(payload: ReminderEvent) => void>();
const orderFactListeners = new Set<(sessionId: string, payload: OrderFactEvent) => void>();
const debateListeners = new Set<(payload: DebateDeltaEvent) => void>();

/** 多 AI 研讨 (feed.debate, 2026-10-24): one turn's (turn, side) or one analyst's report (analyst) text so far while
 *  the model writes it — the whole text each time, like chat's message.delta. The checked version follows on the
 *  debate's next read. */
export interface DebateDeltaEvent { id: string; turn?: number; side?: "bull" | "bear"; analyst?: "technical" | "supply" | "flow" | "news"; text: string }

/** A monitoring strategy's signal changed (feed.strategy-signal): the payload the Runtime's
 *  monitor composed — conclusion, spoken line and the same annotations explain_strategy_signal
 *  draws. The client owns how it is shown; nothing here touches orders. */
export interface StrategySignalEvent {
  strategyId?: string;
  name?: string;
  symbol?: string;
  action?: string;
  actionLabel?: string;
  volume?: number;
  barTime?: number | null;
  annotations?: unknown[];
  spoken?: string;
}

/** A price alert crossed (feed.price-alert, 2026-10-02): every subscribed window of the user's
 *  gets one. The workspace announces it (its own notify preference) and the alerts handle
 *  re-reads the lists, which drops the alert's chart line and list row. */
export interface PriceAlertEvent {
  id?: string;
  symbol?: string;
  name?: string;
  direction?: "above" | "below";
  price?: string;
  lastPrice?: number | null;
  note?: string;
  text?: string;
  triggeredAt?: string | null;
}

/** 统一提醒 (feed.reminder; 2026-10-03 收盘前/风险度, #932 2026-10-15 every kind): the Runtime already
 * gated it on that kind's own switch; every subscribed window of the user's shows the desktop
 * notification (titled by `title`) and the light in-app notice, and the 提醒中心 re-reads the day.
 * `type: "update"` is a recorded reminder whose state changed (平仓单成交了 / 保本已移) — re-read, no
 * notification. #457: accountId/accountName carry which account the reminder belongs to (absent for
 * a single-account user or an older Runtime). */
export interface ReminderEvent {
  /** The 提醒中心 record (`GET /api/reminders` item id) and the user's unread count after it. */
  id?: string;
  type?: "new" | "update";
  unread?: number;
  kind?: string;
  /** 多 AI 研讨's completion: which debate ended. */
  debateId?: string;
  title?: string;
  text?: string;
  at?: string;
  accountId?: string;
  accountName?: string;
}

/** A watched simulation order's read-back changed (feed.order, 2026-10-08): the same order view
 *  the list endpoint serves, pushed to the session whose card was confirmed — a confirmed card's
 *  per-order rows and the orders screen follow fills and cancels without waiting for a refresh. */
export interface OrderFactEvent {
  clientOrderId?: string;
  accountId?: string;
  displayStatus?: string;
  [field: string]: unknown;
}

function emit() { listeners.forEach(listener => listener()); }

function text(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined;
}

/** The Runtime's capture time inside a channel payload, where that channel carries one. */
function sourceTime(channel: FeedChannel, payload: Record<string, unknown>): string | undefined {
  if (channel === "market") return text((payload.quote as Record<string, unknown> | undefined)?.capturedAt);
  if (channel === "account") return text((payload.account as Record<string, unknown> | undefined)?.capturedAt);
  return text(payload.capturedAt);
}

/** A copy of `frame` with each [path, value] stamp written in (the path's containers are copied). */
function withStamps(frame: Record<string, unknown>, stamps: [Array<string | number>, unknown][]): Record<string, unknown> {
  const root: Record<string, unknown> = { ...frame };
  for (const [path, value] of stamps) {
    let node: Record<string | number, unknown> = root;
    for (const key of path.slice(0, -1)) {
      const child = node[key];
      if (child === null || typeof child !== "object") { node = {}; break; }
      node[key] = Array.isArray(child) ? [...child] : { ...(child as Record<string, unknown>) };
      node = node[key] as Record<string | number, unknown>;
    }
    if (path.length) node[path[path.length - 1]] = value;
  }
  return root;
}

/** Route a stream event into the feed; returns true when it was a feed event. */
export function applyFeedEvent(sessionId: string, event: UiEvent, now = Date.now()): boolean {
  if (!event.name.startsWith("feed.")) return false;
  if (event.name === "feed.ui") {
    const reason = String((event.payload as { reason?: unknown } | undefined)?.reason ?? "");
    nudgeListeners.forEach(listener => listener(reason));
    return true;
  }
  if (event.name === "feed.market-patch") {
    const patch = event.payload;
    const previous = states.get(sessionId) ?? EMPTY;
    const market = previous.market;
    if (!market?.revision || market.revision !== patch.base || market.durationSeconds !== patch.durationSeconds
        || previous.channels.market?.status !== "ok") {
      // Missed the frame this patch builds on (a slow reader keeps only the latest market event):
      // ask for a full frame again instead of drawing a wrong chart.
      resyncListeners.forEach(listener => listener(sessionId));
      return true;
    }
    const merged = mergeGuestMarket(market, { workbench: patch.changes as GuestMarketFrame["workbench"] }, false);
    const next: RuntimeFeedState = { ...previous, sessionId, market: { ...merged!, revision: patch.revision },
      channels: { ...previous.channels, market: { receivedAt: now, status: "ok", sourceAt: sourceTime("market", merged as unknown as Record<string, unknown>) } } };
    states.set(sessionId, next);
    emit();
    if (sessionId === activeSessionId) rumPriceFrame(next.channels.market?.sourceAt);
    return true;
  }
  if (event.name === "feed.board-patch") {
    const patch = event.payload;
    const previous = states.get(sessionId) ?? EMPTY;
    const board = previous.board;
    if (!board || board.feedRevision !== patch.base || board.key !== patch.key || previous.channels.board?.status !== "ok") {
      // Not the board this patch builds on: send the view again for one full frame.
      boardResyncListeners.forEach(listener => listener(sessionId));
      return true;
    }
    const changes = patch.rows as Record<string, Partial<MarketBoardRow>>;
    const rows = Object.keys(changes).length
      ? board.rows.map(row => changes[row.symbol] ? { ...row, ...changes[row.symbol] } : row) : board.rows;
    const next: FeedBoardSnapshot = { ...board, ...(patch.meta as Partial<MarketBoardSnapshot>), rows, feedRevision: patch.feedRevision };
    states.set(sessionId, { ...previous, sessionId, board: next,
      channels: { ...previous.channels, board: { receivedAt: now, status: "ok", sourceAt: next.capturedAt } } });
    emit();
    return true;
  }
  if (event.name === "feed.touch") {
    // The channel's last frame is still current; only its capture stamps moved.
    const channel = CHANNEL_OF[event.payload.name];
    const previous = states.get(sessionId) ?? EMPTY;
    const frame = channel ? previous[channel] as unknown as Record<string, unknown> | undefined : undefined;
    if (!channel || !frame) return true;   // nothing to refresh yet: the next full frame brings it
    const refreshed = withStamps(frame, event.payload.stamps);
    states.set(sessionId, { ...previous, sessionId, [channel]: refreshed,
      channels: { ...previous.channels, [channel]: { receivedAt: now, status: "ok", sourceAt: sourceTime(channel, refreshed) } } });
    emit();
    return true;
  }
  if (event.name === "feed.strategy-signal") {
    signalListeners.forEach(listener => listener((event.payload ?? {}) as StrategySignalEvent));
    return true;
  }
  if (event.name === "feed.price-alert") {
    priceAlertListeners.forEach(listener => listener((event.payload ?? {}) as PriceAlertEvent));
    return true;
  }
  if (event.name === "feed.reminder") {
    reminderListeners.forEach(listener => listener((event.payload ?? {}) as ReminderEvent));
    return true;
  }
  if (event.name === "feed.order") {
    orderFactListeners.forEach(listener => listener(sessionId, (event.payload ?? {}) as OrderFactEvent));
    return true;
  }
  if (event.name === "feed.debate") {
    debateListeners.forEach(listener => listener(event.payload as DebateDeltaEvent));
    return true;
  }
  let payload = (event.payload ?? {}) as Record<string, unknown>;
  if (event.name === "feed.market" && payload.barsAfter !== undefined) {
    // The focus's first frame carries only the bars after the ones this browser offered (`since`).
    const whole = feedMarketFrame(sessionId, payload as unknown as MarketWorkbenchSnapshot);
    if (!whole) {
      // The held bars failed the proof: ask again for the whole window.
      resyncListeners.forEach(listener => listener(sessionId));
      return true;
    }
    payload = whole as unknown as Record<string, unknown>;
  }
  const unavailable = event.name === "feed.status";
  const channel = unavailable ? CHANNEL_OF[`feed.${String(payload.channel)}`] : CHANNEL_OF[event.name];
  if (!channel) return true;
  const previous = states.get(sessionId) ?? EMPTY;
  const entry: FeedChannelState = unavailable
    ? { receivedAt: now, status: "unavailable", error: text(payload.reason) }
    : { receivedAt: now, status: "ok", sourceAt: sourceTime(channel, payload) };
  // An unavailable channel keeps whatever snapshot it last delivered; it simply
  // stops counting as live, so that one reader falls back to its own poll.
  const next: RuntimeFeedState = { ...previous, sessionId, channels: { ...previous.channels, [channel]: entry } };
  if (!unavailable) next[channel] = payload as never;
  states.set(sessionId, next);
  emit();
  if (channel === "market" && !unavailable && sessionId === activeSessionId) rumPriceFrame(entry.sourceAt);
  return true;
}

/** A session (re)attached its stream: drop its stale snapshots and make it the window's active session. */
export function resetFeed(sessionId?: string) {
  if (sessionId) { states.delete(sessionId); streamAliveAt.delete(sessionId); } else { states.clear(); streamAliveAt.clear(); }
  activeSessionId = sessionId;
  keepFeedFocusOf(sessionId); // other sessions' offered chart bars are not needed any more (#850)
  emit();
}

const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };

/** Snapshots for one session, or for the window's active session when none is given. */
export function useRuntimeFeed(sessionId?: string): RuntimeFeedState {
  return useSyncExternalStore(subscribe, () => states.get(sessionId ?? activeSessionId ?? "") ?? EMPTY);
}

/** The active session's pushed board and its channel state only: a board reader re-renders on board
 *  events, not on every account or market frame. */
export function useFeedBoard(): { board?: FeedBoardSnapshot; channel?: FeedChannelState } {
  const board = useSyncExternalStore(subscribe, () => states.get(activeSessionId ?? "")?.board);
  const channel = useSyncExternalStore(subscribe, () => states.get(activeSessionId ?? "")?.channels.board);
  return { board, channel };
}

/** Non-reactive, for poll timers: does the active session's push deliver the board rows named `key` now? */
export function feedBoardLiveNow(key: string, now = Date.now()): boolean {
  const state = states.get(activeSessionId ?? "") ?? EMPTY;
  return Boolean(key) && state.board?.key === key && feedIsLive(state, "board", now);
}

/** The session's stream showed life (any frame, the idle heartbeat included). No re-render. */
export function markFeedStreamAlive(sessionId: string, now = Date.now()) {
  streamAliveAt.set(sessionId, now);
}

/** Is this one channel delivering? Another channel's traffic never answers for it. */
export function feedIsLive(state: RuntimeFeedState, channel: FeedChannel, now = Date.now()): boolean {
  const entry = state.channels[channel];
  if (entry === undefined || entry.status !== "ok") return false;
  if (now - entry.receivedAt < FEED_STALE_MS) return true;
  // Pushed on change only: an unchanged market stays live while its stream is alive.
  return channel === "market" && state.sessionId !== undefined
    && now - (streamAliveAt.get(state.sessionId) ?? -Infinity) < STREAM_ALIVE_MS;
}

/**
 * Non-reactive check for timers: is the active session's feed delivering? With a
 * channel it answers for that channel only; without one it answers "the stream is
 * carrying something", which is what the UI-pulse and list pollers pace against.
 */
export function feedLiveNow(channel?: FeedChannel): boolean {
  const state = states.get(activeSessionId ?? "") ?? EMPTY;
  return (channel ? [channel] : FEED_CHANNELS).some(name => feedIsLive(state, name));
}

/** A market patch did not fit the market this session holds: re-send the feed focus for a full frame. */
export function onMarketResync(listener: (sessionId: string) => void): () => void {
  resyncListeners.add(listener);
  return () => { resyncListeners.delete(listener); };
}

/** A board patch did not fit the board this session holds: send the feed view again for a full frame. */
export function onBoardResync(listener: (sessionId: string) => void): () => void {
  boardResyncListeners.add(listener);
  return () => { boardResyncListeners.delete(listener); };
}

/** The Runtime queued a UI command or capture (reason "command" / "capture"): pulse now instead of
 *  waiting for the idle interval. 画线第二版: reason "drawings" — the user's drawings changed. */
export function onFeedNudge(listener: (reason: string) => void): () => void {
  nudgeListeners.add(listener);
  return () => { nudgeListeners.delete(listener); };
}

/** A monitoring strategy's signal changed: draw the annotations and show the light notice. */
export function onStrategySignal(listener: (payload: StrategySignalEvent) => void): () => void {
  signalListeners.add(listener);
  return () => { signalListeners.delete(listener); };
}

export function onPriceAlert(listener: (payload: PriceAlertEvent) => void): () => void {
  priceAlertListeners.add(listener);
  return () => { priceAlertListeners.delete(listener); };
}

export function onReminder(listener: (payload: ReminderEvent) => void): () => void {
  reminderListeners.add(listener);
  return () => { reminderListeners.delete(listener); };
}

export function onDebateDelta(listener: (payload: DebateDeltaEvent) => void): () => void {
  debateListeners.add(listener);
  return () => { debateListeners.delete(listener); };
}

/** A watched order's read-back changed in that session: merge the fact into the orders list. */
export function onOrderFact(listener: (sessionId: string, payload: OrderFactEvent) => void): () => void {
  orderFactListeners.add(listener);
  return () => { orderFactListeners.delete(listener); };
}

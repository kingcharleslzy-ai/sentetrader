import { useCallback, useEffect, useRef, useState } from "react";
import type { CardAction } from "@ai-trader/ui-events";
import { requestJson } from "./api";
import { onReminder } from "./runtimeFeed";

/**
 * 统一提醒 · 提醒记录 (#932, 2026-10-15): the reminders the Runtime recorded for this user — one list
 * the desktop bell, the phone's 提醒 page and the AI tab badge read, with read marks kept on the
 * Runtime (`GET /api/reminders`, `POST /api/reminders/read`), so a reminder read on one device is
 * read on every one. `feed.reminder` (a new reminder or an update of one's state) re-reads the day.
 * This module is the thin client of those two routes; deciding *whether* to remind is the Runtime's.
 */

/** A row's result line (平仓单 已成交 771.5 ×1 / 仍在排队 0/1 / 止损已移到 3,060). */
export interface ReminderState { text: string; tone?: "ok" | "wait" | "warn" | "muted" | null }
/** Where a row opens: the conversation's card, 委托, 调研, 新闻 or 复盘. */
export interface ReminderTarget {
  type: "chat" | "orders" | "research" | "news" | "review";
  cardId?: string | null;
  sessionId?: string | null;
  symbol?: string | null;
  debateId?: string | null;
  tradingDay?: string | null;
  /** 今日作战表 (2026-10-25): the 调研 page a research row opens; absent = 品种调研. */
  page?: "sheet" | null;
}
export interface ReminderItem {
  id: string;
  kind: string;
  tradingDay: string;
  createdAt: string;
  /** The trading account an account reminder belongs to (成交 / 撤单 / 挂单 / 复盘); null for user-level ones. */
  accountId?: string | null;
  symbol?: string | null;
  /** The contract (or 账户 / 品种组) the row names, in words: “螺纹2701 · 沪金2612”. */
  name?: string | null;
  title: string;
  body: string;
  state?: ReminderState | string | null;
  target?: ReminderTarget | null;
  read: boolean;
  /** The card's own answer buttons while it still waits (保本: 移到成本 / 不用) — never an order. */
  actions?: CardAction[] | null;
}
export interface ReminderPage {
  items: ReminderItem[]; unread: number; tradingDay: string | null; nextCursor: string | null;
}

export async function listReminders(baseUrl: string, query: { scope: "today" | "earlier"; before?: string | null; unread?: boolean }) {
  const params = new URLSearchParams({ scope: query.scope });
  if (query.before) params.set("before", query.before);
  if (query.unread) params.set("unread", "1");
  const page = await requestJson<Partial<ReminderPage>>(`${baseUrl}/api/reminders?${params}`);
  return { items: Array.isArray(page.items) ? page.items : [], unread: Number(page.unread) || 0,
    tradingDay: page.tradingDay ?? null, nextCursor: page.nextCursor ?? null } satisfies ReminderPage;
}

/** 今天的提醒全部读出 (#932 P2): the Runtime pages 50 at a time; a busy day's rows past the first page used to
 *  have no way in (查看更早 reads the earlier days only). Bounded, so a runaway day cannot loop: past
 *  TODAY_MAX_PAGES pages the page keeps `nextCursor`, and reading on from it (`before`) is the next batch (#932 R5). */
export const TODAY_MAX_PAGES = 10;
export async function listTodayReminders(baseUrl: string, before: string | null = null): Promise<ReminderPage> {
  const first = await listReminders(baseUrl, { scope: "today", before });
  const items = [...first.items];
  let cursor = first.nextCursor;
  for (let page = 1; cursor && page < TODAY_MAX_PAGES; page += 1) {
    const next = await listReminders(baseUrl, { scope: "today", before: cursor });
    items.push(...next.items);
    cursor = next.nextCursor;
  }
  return { ...first, items, nextCursor: cursor };
}

export async function markRemindersRead(baseUrl: string, which: { ids: string[] } | { all: true }) {
  return requestJson<{ unread: number }>(`${baseUrl}/api/reminders/read`, { method: "POST", body: JSON.stringify(which) });
}

/** What a row says in its state pill, and the pill's tone. */
export function reminderState(state: ReminderItem["state"]): ReminderState | null {
  if (!state) return null;
  if (typeof state === "string") return state.trim() ? { text: state } : null;
  return state.text ? state : null;
}

const SHANGHAI = "Asia/Shanghai";
const clockFormat = new Intl.DateTimeFormat("zh-CN", { timeZone: SHANGHAI, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const dayFormat = new Intl.DateTimeFormat("zh-CN", { timeZone: SHANGHAI, month: "2-digit", day: "2-digit" });

/** "14:50" in exchange time; "" when the time cannot be read. */
export function reminderClock(createdAt: string): string {
  const date = new Date(createdAt);
  return Number.isNaN(date.valueOf()) ? "" : clockFormat.format(date);
}

function shanghaiMinutes(date: Date): number {
  const [hour, minute] = clockFormat.format(date).split(":").map(Number);
  return hour * 60 + minute;
}

/** "10月15日 交易日" from "2026-10-15". */
export function tradingDayLabel(day: string | null | undefined): string {
  const match = /^\d{4}-(\d{2})-(\d{2})$/.exec(day ?? "");
  return match ? `${Number(match[1])}月${Number(match[2])}日 交易日` : "今天";
}

/** The part of the trading day a reminder fell in: the night session that opened it (21:00–02:30,
 *  named by its evening), before the open, the morning and the afternoon (the close's 15:15 复盘 too). */
export function sessionGroup(createdAt: string): { key: string; label: string } {
  const date = new Date(createdAt);
  if (Number.isNaN(date.valueOf())) return { key: "other", label: "其他" };
  const minutes = shanghaiMinutes(date);
  if (minutes >= 20 * 60 || minutes < 3 * 60) {
    // An after-midnight reminder belongs to the previous evening's night session.
    const evening = new Date(date.getTime() - (minutes < 3 * 60 ? 4 * 3_600_000 : 0));
    const [month, day] = dayFormat.format(evening).split("/");
    return { key: `night:${month}-${day}`, label: `夜盘 · ${month}-${day} 晚` };
  }
  if (minutes < 9 * 60) return { key: "pre", label: "盘前" };
  if (minutes < 12 * 60) return { key: "am", label: "日盘 · 上午" };
  return { key: "pm", label: "日盘 · 下午" };
}

export interface ReminderGroup { key: string; label: string; items: ReminderItem[] }

/** Newest first, split where the session changes (today) or where the trading day changes (更早). */
export function groupReminders(items: readonly ReminderItem[], by: "session" | "day"): ReminderGroup[] {
  const sorted = [...items].sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt));
  const groups: ReminderGroup[] = [];
  for (const item of sorted) {
    const { key, label } = by === "session" ? sessionGroup(item.createdAt) : { key: item.tradingDay, label: tradingDayLabel(item.tradingDay) };
    const last = groups.at(-1);
    if (last?.key === key) last.items.push(item);
    else groups.push({ key, label, items: [item] });
  }
  return groups;
}

export type ReminderFilter = "unread" | "all";

export interface RemindersHandle {
  /** False for a guest or while disconnected: no bell, no page, no badge. */
  available: boolean;
  status: "idle" | "loading" | "ready" | "error";
  error: string;
  tradingDay: string | null;
  today: ReminderItem[];
  /** 当天还有没读出来的 (#932 R5): a busy day past TODAY_MAX_PAGES pages; `loadMoreToday` reads on from there. */
  hasMoreToday: boolean;
  /** Why the last 继续加载当天提醒 / 查看更早 failed (shown beside that control, which stays to retry); "" when it did not. */
  moreError: string;
  earlierError: string;
  /** 查看更早: the earlier trading days read so far (newest first) and whether more remain. */
  earlier: ReminderItem[];
  earlierShown: boolean;
  hasMoreEarlier: boolean;
  /** Every unread reminder of the user's, as the Runtime counts them (the badge). */
  unread: number;
  /** Reads the next batch of today; resolves "" once it landed, else why nothing was loaded. */
  loadMoreToday: () => Promise<string>;
  loadEarlier: () => Promise<void>;
  markRead: (ids: string[]) => void;
  markAllRead: () => void;
  refresh: () => void;
}

const REFRESH_DEBOUNCE_MS = 250;
const NOTHING_MORE = "当天的提醒已经全部读出，没有更多可加载";

function readError(error: unknown): string {
  const status = (error as { status?: number }).status;
  if (status === 404) return "服务端还没有提醒记录";
  return "提醒读取失败，稍后会再试";
}

function appendNew(current: ReminderItem[], items: ReminderItem[]): ReminderItem[] {
  const seen = new Set(current.map(item => item.id));
  return [...current, ...items.filter(item => !seen.has(item.id))];
}

/**
 * The reads and read marks of 提醒记录 (#932 R6): every request that may set the unread count takes a ticket and only
 * the newest one sets it, so an older answer — a read that started before a 标读, a 标读 overtaken by 全部已读 — never
 * overwrites a newer count. The marks run one after another, and a read waits for the marks already sent, so what
 * the Runtime answers is never older than what this window marked. Rows an older read returns keep the marks made
 * after it started. A Runtime / sign-in change starts a new generation: nothing of the old one lands.
 */
export function useReminders(baseUrl: string | undefined, enabled: boolean): RemindersHandle {
  const available = Boolean(baseUrl) && enabled;
  const [today, setToday] = useState<ReminderItem[]>([]);
  const [todayCursor, setTodayCursor] = useState<string | null>(null);
  const [earlier, setEarlier] = useState<ReminderItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [earlierShown, setEarlierShown] = useState(false);
  const [unread, setUnread] = useState(0);
  const [tradingDay, setTradingDay] = useState<string | null>(null);
  const [status, setStatus] = useState<RemindersHandle["status"]>("idle");
  const [error, setError] = useState("");
  const [moreError, setMoreError] = useState("");
  const [earlierError, setEarlierError] = useState("");
  const [readKey, setReadKey] = useState(0);
  const generation = useRef(0);
  // Today's list reads — every refresh and every 继续加载 — run one after another (Codex r4308006068), each on the
  // cursor and day the one before it left (`todayAt`, `day`).
  const lists = useRef<Promise<unknown>>(Promise.resolve());
  const todayAt = useRef<string | null>(null);
  // Tickets of every request that may set the unread count; the newest sets it.
  const ticket = useRef(0);
  // Marks made in this generation (id → ticket; 全部已读's ticket): applied to rows a read started before returns.
  const marks = useRef({ ids: new Map<string, number>(), all: 0 });
  // The read marks, one after another; reads wait for those already sent.
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  // The trading day of the list and the 查看更早 pages read for it: a new day starts 更早 over.
  const day = useRef<string | null>(null);
  const earlierEpoch = useRef(0);

  const afterMarks = useCallback(<T,>(task: () => Promise<T>) => queue.current.then(task), []);
  const inTurn = useCallback(<T,>(task: () => Promise<T>) => {
    const run = lists.current.then(task);
    lists.current = run.catch(() => undefined);
    return run;
  }, []);
  const overlay = useCallback((items: ReminderItem[], since: number) => items.map(item =>
    !item.read && (marks.current.all > since || (marks.current.ids.get(item.id) ?? 0) > since) ? { ...item, read: true } : item), []);

  // The Runtime this generation reads and marks on: a disconnect or another Runtime starts a new generation with a
  // queue of its own — a mark still pending on the old connection never holds the new reads (Codex r4307899295), and
  // nothing it answers lands (every answer checks its generation).
  const scope = useRef<string | undefined>(undefined);
  const restart = useCallback((next: string | undefined) => {
    scope.current = next;
    generation.current += 1;
    queue.current = Promise.resolve();
    marks.current = { ids: new Map(), all: 0 };
    lists.current = Promise.resolve();
    todayAt.current = null;
  }, []);

  useEffect(() => {
    const target = available ? baseUrl : undefined;
    if (scope.current !== target) restart(target);
    if (!available || !baseUrl) {
      day.current = null;
      setMoreError(""); setEarlierError("");
      setToday([]); setTodayCursor(null); setEarlier([]); setEarlierShown(false); setCursor(null); setUnread(0); setTradingDay(null); setStatus("idle");
      return;
    }
    const gen = generation.current;
    const since = ++ticket.current;
    setStatus(current => current === "ready" ? current : "loading");
    inTurn(() => afterMarks(() => listTodayReminders(baseUrl)))
      .then(page => {
        if (gen !== generation.current) return;
        todayAt.current = page.nextCursor;
        setToday(overlay(page.items, since)); setTodayCursor(page.nextCursor); setTradingDay(page.tradingDay); setStatus("ready"); setError(""); setMoreError("");
        if (since === ticket.current) setUnread(page.unread);
        if (page.tradingDay && day.current && page.tradingDay !== day.current) {
          // A new trading day: yesterday's list is history now — 查看更早 reads from the top again.
          earlierEpoch.current += 1;
          setEarlier([]); setCursor(null); setEarlierShown(false); setEarlierError("");
        }
        if (page.tradingDay) day.current = page.tradingDay;
      })
      .catch(reason => { if (gen === generation.current) { setStatus("error"); setError(readError(reason)); } });
  }, [available, baseUrl, readKey, afterMarks, overlay, restart, inTurn]);

  const refresh = useCallback(() => setReadKey(key => key + 1), []);
  // feed.reminder: a new reminder or a changed state; a burst re-reads once.
  useEffect(() => {
    if (!available) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const off = onReminder(() => { clearTimeout(timer); timer = setTimeout(refresh, REFRESH_DEBOUNCE_MS); });
    return () => { clearTimeout(timer); off(); };
  }, [available, refresh]);

  // 继续加载当天提醒: queued behind any refresh; when its turn comes it reads on only from the cursor and day it was
  // asked on — a refresh in between moved them, and reading from the old cursor would skip rows (Codex r4308006068).
  const loadMoreToday = useCallback(async (): Promise<string> => {
    if (!baseUrl || !available) return "提醒记录还没连上";
    const asked = { cursor: todayAt.current, day: day.current };
    if (!asked.cursor) return NOTHING_MORE;
    const gen = generation.current;
    setMoreError("");
    return inTurn(async () => {
      if (gen !== generation.current) return "提醒记录重新连接过，没有加载";
      if (todayAt.current !== asked.cursor || day.current !== asked.day) {
        const reason = todayAt.current ? "当天提醒刚刷新过，再点一次接着读" : NOTHING_MORE;
        setMoreError(todayAt.current ? reason : "");
        return reason;
      }
      // Its unread ticket only now, when it really reads (Codex r4308078553): one turned away never mutes the count
      // of the refresh queued before it.
      const since = ++ticket.current;
      try {
        const page = await afterMarks(() => listTodayReminders(baseUrl, asked.cursor));
        if (gen !== generation.current) return "提醒记录重新连接过，没有加载";
        todayAt.current = page.nextCursor;
        setToday(current => appendNew(current, overlay(page.items, since))); setTodayCursor(page.nextCursor);
        if (since === ticket.current) setUnread(page.unread);
        return "";
      } catch (reason) {
        const text = readError(reason);
        if (gen === generation.current) setMoreError(text);
        return text;
      }
    });
  }, [available, baseUrl, afterMarks, overlay, inTurn]);

  const loadEarlier = useCallback(async () => {
    if (!baseUrl || !available) return;
    const before = earlierShown ? cursor : null;
    if (earlierShown && !cursor) return;
    const gen = generation.current;
    const epoch = earlierEpoch.current;
    const since = ++ticket.current;
    setEarlierError("");
    try {
      const page = await afterMarks(() => listReminders(baseUrl, { scope: "earlier", before }));
      if (gen !== generation.current || epoch !== earlierEpoch.current) return;
      setEarlier(current => appendNew(current, overlay(page.items, since)));
      setCursor(page.nextCursor); setEarlierShown(true);
      if (since === ticket.current) setUnread(page.unread);
    } catch (reason) { if (gen === generation.current && epoch === earlierEpoch.current) setEarlierError(readError(reason)); }
  }, [available, baseUrl, cursor, earlierShown, afterMarks, overlay]);

  const mark = useCallback((which: { ids: string[] } | { all: true }) => {
    if (!baseUrl) return;
    const gen = generation.current;
    const mine = ++ticket.current;
    if ("all" in which) marks.current.all = mine;
    else for (const id of which.ids) marks.current.ids.set(id, mine);
    const sent = afterMarks(() => markRemindersRead(baseUrl, which));
    queue.current = sent.catch(() => undefined);
    sent.then(result => { if (gen === generation.current && mine === ticket.current) setUnread(Number(result.unread) || 0); })
      .catch(() => { if (gen === generation.current) refresh(); });
  }, [baseUrl, afterMarks, refresh]);

  const markRead = useCallback((ids: string[]) => {
    if (!baseUrl || !ids.length) return;
    const wanted = new Set(ids);
    const fresh = [...today, ...earlier].filter(item => wanted.has(item.id) && !item.read).length;
    if (!fresh) return;
    const read = (items: ReminderItem[]) => items.map(item => wanted.has(item.id) ? { ...item, read: true } : item);
    setToday(read); setEarlier(read); setUnread(count => Math.max(0, count - fresh));
    mark({ ids });
  }, [baseUrl, today, earlier, mark]);

  const markAllRead = useCallback(() => {
    if (!baseUrl) return;
    const read = (items: ReminderItem[]) => items.map(item => item.read ? item : { ...item, read: true });
    setToday(read); setEarlier(read); setUnread(0);
    mark({ all: true });
  }, [baseUrl, mark]);

  return { available, status, error, tradingDay, today, hasMoreToday: Boolean(todayCursor), moreError, earlierError, earlier, earlierShown,
    hasMoreEarlier: !earlierShown || Boolean(cursor), unread: available ? unread : 0,
    loadMoreToday, loadEarlier, markRead, markAllRead, refresh };
}

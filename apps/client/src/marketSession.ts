import type { MarketQuoteSnapshot, MarketWorkbenchSnapshot } from "./api";

/** The same 15 s quote-staleness bar the Runtime's order boundary enforces (dual clock). */
export const QUOTE_STALE_SECONDS = 15;

/** A quote's age in seconds. Sidecar stamps are naive Beijing time; ISO stamps carry their own zone. */
export function quoteAgeSeconds(quoteDatetime: string | undefined, now = Date.now()): number | undefined {
  const text = (quoteDatetime ?? "").trim();
  if (!text) return undefined;
  const normalized = text.includes("T") ? text : text.replace(" ", "T");
  const zoned = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(normalized);
  const parsed = Date.parse(zoned ? normalized : `${normalized}+08:00`);
  return Number.isNaN(parsed) ? undefined : Math.max(0, (now - parsed) / 1000);
}

export function sessionSeconds(value: string): number {
  const parts = value.split(":").map(Number);
  return parts[0] * 3600 + parts[1] * 60 + (parts[2] || 0);
}

export function sessionTime(value: string): string {
  const seconds = sessionSeconds(value);
  return `${seconds >= 86400 ? "次日 " : ""}${String(Math.floor(seconds / 3600) % 24).padStart(2, "0")}:${String(Math.floor(seconds / 60) % 60).padStart(2, "0")}`;
}

/** 外盘（CME）的时段 (外盘盘点 D 2026-10-21; per product F2): a tradable 外盘 chart's snapshot carries the product's CME
 *  hours (`dayRule: "cme"`, the Runtime's cme_schedule — most products one period from the 美东 18:00 open to the 17:00
 *  close, CBOT grains two, lean hogs a day-only one): each period's Beijing start / end ("次日 02:20" past midnight),
 *  the day's first open and last close as "HH:MM"; undefined for every other schedule. */
export interface CmeHours { periods: { start: string; end: string }[]; opens: string; closes: string }
export function cmeHours(schedule: MarketQuoteSnapshot["tradingSchedule"]): CmeHours | undefined {
  const day = schedule?.dayRule === "cme" ? schedule.day : [];
  if (!day.length) return undefined;
  return { periods: day.map(([start, end]) => ({ start: sessionTime(start), end: sessionTime(end) })),
    opens: sessionTime(day[0][0]).replace("次日 ", ""), closes: sessionTime(day[day.length - 1][1]).replace("次日 ", "") };
}

/** The "?" text beside a 外盘 chart's CME hours (交易时间卡, 合约信息卡). */
export const CME_HOURS_HELP = "CME Globex 时段，按品种取自 CME Group 合约细则：多数品种美东 18:00 开盘、次日 17:00 收盘；美豆、豆粕、豆油、玉米、小麦（CBOT）芝加哥时间 19:00–次日 07:45 与 08:30–13:20 两段；瘦肉猪只在芝加哥时间 08:30–13:05。北京时间随美国夏令时变；节假日按 CME 公布的节假日时段（全天休市、提前收盘，美国节日当天的盘并入下一个交易日）。";

const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

/** When a shut CME market opens again (the snapshot's `sessionReopensAt`), as "06:00" the same Beijing day or "周一 06:00". */
function reopenText(reopensAt: string | undefined, capturedAt: string): string {
  const at = Date.parse(reopensAt ?? "");
  if (Number.isNaN(at)) return "";
  const beijing = new Date(at + 8 * 3600_000);
  const today = new Date((Date.parse(capturedAt) || Date.now()) + 8 * 3600_000).toISOString().slice(0, 10);
  const time = beijing.toISOString().slice(11, 16);
  return beijing.toISOString().slice(0, 10) === today ? time : `${WEEKDAYS[beijing.getUTCDay()]} ${time}`;
}

/** Whether the chart's market is shut now — the snapshot's session status (外盘 charts: by CME's clock, the Runtime's). */
export function marketClosedNow(market: MarketWorkbenchSnapshot | undefined): boolean {
  return Boolean(market) && (market!.marketSessionStatus ?? market!.quote.marketSessionStatus) === "closed";
}

/** The chart quote's part of the 下单票's conditions (the first unmet one, or undefined); the Runtime re-checks every
 *  one. A 外盘 chart's snapshot already carries CME's status and the MT5 month quote its order is checked on. */
export function chartQuoteBlockedReason(market: MarketWorkbenchSnapshot, replayActive = false, now = Date.now()): string | undefined {
  if (market.marketSessionStatus === "closed") {
    return !replayActive && cmeHours(market.quote.tradingSchedule) ? `${marketSessionDisplay(market).label}，暂不可下单` : "休市，暂不可下单";
  }
  if (market.dataStatus !== "normal" || market.isCached) return "行情不是实时数据";
  // The same bar the session card labels with (quoteDatetime first; the snapshot's age is the
  // fallback when the stamp cannot be read). A replay's stamps are historical: its own clock
  // (the snapshot's age) decides, and the Runtime re-checks on that clock too.
  const quoteAge = replayActive ? market.ageSeconds : quoteAgeSeconds(market.quote.quoteDatetime, now) ?? market.ageSeconds;
  if (quoteAge > QUOTE_STALE_SECONDS) return `行情已 ${Math.round(quoteAge)} 秒无新报价`;
  return undefined;
}

export function marketSessionDisplay(market?: MarketWorkbenchSnapshot, replayActive = false) {
  const status = market?.marketSessionStatus ?? market?.quote.marketSessionStatus;
  if (replayActive && status === "closed") return { label: "回放已停止", detail: "回放已停止" };
  const fallback = status === "closed" ? "休市" : status === "trading" ? "交易中" : "时段待确认";
  if (market && !replayActive && cmeHours(market.quote.tradingSchedule)) {
    // 外盘 (CME): why it is shut and when it opens again come with the snapshot.
    const reopens = reopenText(market.quote.sessionReopensAt, market.capturedAt);
    const closedFor = status === "closed" ? market.quote.sessionClosedFor : undefined;
    if (closedFor === "intraday") return { label: "盘中休息", detail: `盘中休息 · ${reopens} 恢复` };
    if (closedFor === "break") return { label: "CME 每日停盘", detail: `CME 每日停盘 · ${reopens} 恢复` };
    if (closedFor === "weekend") return { label: "周末休市", detail: `周末休市 · ${reopens} 开盘` };
    if (closedFor === "holiday") return { label: "CME 休市", detail: `CME 节假日休市 · ${reopens} 开盘` };
    return { label: fallback, detail: fallback };
  }
  // Use the snapshot clock so replay never borrows today's wall clock.
  const date = market ? new Date(market.capturedAt) : undefined;
  if (!date || !Number.isFinite(date.getTime())) return { label: fallback, detail: fallback };
  const china = new Date(date.getTime() + 8 * 3600_000);
  const seconds = china.getUTCHours() * 3600 + china.getUTCMinutes() * 60 + china.getUTCSeconds();
  const day = market?.quote.tradingSchedule?.day ?? [];
  const sameQuoteDay = market?.quote.quoteDatetime.slice(0, 10) === china.toISOString().slice(0, 10);
  // Only identify a break when a current-day quote and closed status agree.
  if (status === "closed" && sameQuoteDay && china.getUTCDay() > 0 && china.getUTCDay() < 6) {
    for (let i = 1; i < day.length; i++) {
      if (seconds >= sessionSeconds(day[i - 1][1]) && seconds < sessionSeconds(day[i][0])) {
        return { label: "盘中休息", detail: `盘中休息 · 常规 ${sessionTime(day[i][0])} 恢复`, breakStart: day[i - 1][1], breakEnd: day[i][0] };
      }
    }
  }
  return { label: fallback, detail: fallback };
}

/** The Runtime's 收盘前持仓检查 runs in the last 5 minutes before each half's final close (day /
 *  night, account_reminders.CLOSE_WINDOW_SECONDS): the next such moment after `now`, as "14:55",
 *  from a contract's trading schedule; null without one. */
export function closeCheckTime(schedule: { day: [string, string][]; night: [string, string][] } | null | undefined, now = Date.now()): string | null {
  const ends = [schedule?.day.at(-1)?.[1], schedule?.night.at(-1)?.[1]].filter((end): end is string => Boolean(end)).map(end => sessionSeconds(end) - 300);
  if (!ends.length) return null;
  const china = new Date(now + 8 * 3600_000);
  const seconds = china.getUTCHours() * 3600 + china.getUTCMinutes() * 60;
  // After midnight a night session's end (e.g. 26:30) is still ahead on the same clock line.
  const clock = seconds < 6 * 3600 ? seconds + 86400 : seconds;
  const next = ends.filter(end => end > clock).sort((a, b) => a - b)[0] ?? Math.min(...ends);
  return sessionTime(`${Math.floor(next / 3600) % 24}:${String(Math.floor(next / 60) % 60).padStart(2, "0")}`);
}

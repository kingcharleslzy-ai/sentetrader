import type { MarketKlineBar, MarketQuoteSnapshot } from "./api";

/**
 * 分时图 (2026-10-02): one trading day's 1-minute closes laid on a uniform minute axis. The axis
 * is synthetic — slot i sits at `TIMESHARE_TIME_BASE + i * 60` — so lightweight-charts spaces every
 * traded minute equally (夜盘在前、日盘在后, no gap between sessions, minutes not yet traded are
 * whitespace points the price line grows into). The clock labels map back through the slot table;
 * no real date is attached to any slot on purpose.
 */
export const TIMESHARE_TIME_BASE = 946684800; // 2000-01-01T00:00:00Z, far from any real bar time.

export type TradingSchedule = NonNullable<MarketQuoteSnapshot["tradingSchedule"]>;

export interface TimeshareSlot {
  /** The synthetic chart time (TIMESHARE_TIME_BASE + index × 60). */
  time: number;
  /** The slot's wall clock "HH:MM" (a crossing night session wraps past 24:00). */
  clock: string;
}

/** A point on the price / average lines; no `value` = whitespace (the minute has not traded). */
export interface TimesharePoint {
  time: number;
  value?: number;
}

/** One minute's volume bar on the same synthetic axis; `up` picks the direction colour. */
export interface TimeshareVolume {
  time: number;
  value: number;
  up: boolean;
}

export interface TimeshareReadout {
  time: number;
  clock: string;
  price: number;
  average?: number;
  volume: number;
  up: boolean;
}

export interface TimeshareData {
  /** The trading day shown (YYYY-MM-DD; a night bar carries the next trading day), null when no bar loaded. */
  tradingDay: string | null;
  slots: TimeshareSlot[];
  price: TimesharePoint[];
  average: TimesharePoint[];
  volume: TimeshareVolume[];
  /** The newest traded minute — the readout row's no-hover values. */
  latest: TimeshareReadout | null;
}

const CHINA_TZ = "Asia/Shanghai";
const clockFormatter = new Intl.DateTimeFormat("en-GB", { timeZone: CHINA_TZ, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const dateFormatter = new Intl.DateTimeFormat("en-CA", { timeZone: CHINA_TZ, year: "numeric", month: "2-digit", day: "2-digit" });
const pad2 = (value: number) => String(value).padStart(2, "0");
const clockOf = (minute: number) => `${pad2(Math.floor(minute / 60) % 24)}:${pad2(minute % 60)}`;

/** The bar's minute of the wall clock in China time (21:05 → 1265; the chart shows the same clock). */
function minuteOfDay(datetimeNanos: number): number {
  const text = clockFormatter.format(new Date(datetimeNanos / 1_000_000));
  const [hour, minute] = text.split(":").map(Number);
  return hour * 60 + minute;
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
function isoShift(iso: string, days: number): string {
  const [year, month, day] = iso.split("-").map(Number);
  const shifted = new Date(Date.UTC(year, month - 1, day + days));
  return `${shifted.getUTCFullYear()}-${pad2(shifted.getUTCMonth() + 1)}-${pad2(shifted.getUTCDate())}`;
}
function advanceToWeekday(iso: string): string {
  const [year, month, day] = iso.split("-").map(Number);
  const weekday = new Date(Date.UTC(year, month - 1, day)).getUTCDay();
  return weekday === 0 ? isoShift(iso, 1) : weekday === 6 ? isoShift(iso, 2) : iso;
}

/**
 * The trading day a bar belongs to when the Runtime sent no `tradingDay`: 夜盘 (≥18:00) rolls to the
 * next weekday, early morning (<06:00, a crossing night session) stays on today advanced off the
 * weekend — Friday 21:00 and Saturday 00:30 both land on Monday. The sidecar's calendar stamp is
 * authoritative whenever present; this only backs an older Runtime up.
 */
function fallbackTradingDay(datetimeNanos: number): string {
  const date = dateFormatter.format(new Date(datetimeNanos / 1_000_000));
  const minute = minuteOfDay(datetimeNanos);
  if (minute >= 18 * 60) return advanceToWeekday(isoShift(date, 1));
  return advanceToWeekday(date);
}

interface GridPeriod {
  start: number;
  span: number;
}

function parsePeriods(list: [string, string][] | undefined): GridPeriod[] {
  return (list ?? [])
    .map(([from, to]) => {
      const start = Number(from.slice(0, 2)) * 60 + Number(from.slice(3, 5));
      const end = Number(to.slice(0, 2)) * 60 + Number(to.slice(3, 5));
      return { start, span: (end - start + 1440) % 1440 };
    })
    .filter(period => period.span > 0);
}

const EMPTY_DATA: TimeshareData = { tradingDay: null, slots: [], price: [], average: [], volume: [], latest: null };

/**
 * The whole 分时图 from the loaded 1-minute bars. `bars` are the workbench's 60 s serial (ascending;
 * ~600 bars hold more than one day, so the day shown is the newest bar's `tradingDay` and everything
 * older drops out). The 均价 line is 当日累计 Σ(close × volume) ÷ Σvolume — an estimate by minute
 * closes, carried across an illiquid skipped minute; both lines turn whitespace past the newest bar.
 */
export function buildTimeshare(
  bars: MarketKlineBar[],
  schedule?: TradingSchedule | null
): TimeshareData {
  const usable = bars.filter(bar => Number.isFinite(bar.close));
  if (!usable.length) return EMPTY_DATA;

  // Every bar the Runtime sends carries its trading day (a 外盘 MT5 bar its CME day, 美东 18:00 换日).
  const tradingDayOf = (bar: MarketKlineBar) =>
    ISO_DATE.test(bar.tradingDay ?? "") ? bar.tradingDay! : fallbackTradingDay(bar.datetime);
  const day = tradingDayOf(usable[usable.length - 1]);
  const todays = usable.filter(bar => tradingDayOf(bar) === day);
  if (!todays.length) return EMPTY_DATA;

  // Night first, then day, in each list's order: a trading day starts at 21:00 the evening before.
  const periods = [...parsePeriods(schedule?.night), ...parsePeriods(schedule?.day)];
  const slots: TimeshareSlot[] = [];
  const slotIndexForMinute = new Map<number, number>();
  if (periods.length) {
    for (const period of periods) {
      const base = slots.length;
      for (let offset = 0; offset < period.span; offset += 1) {
        const minute = (period.start + offset) % 1440;
        if (!slotIndexForMinute.has(minute)) slotIndexForMinute.set(minute, base + offset);
        slots.push({ time: TIMESHARE_TIME_BASE + slots.length * 60, clock: clockOf(minute) });
      }
    }
  } else {
    // No trading schedule (an older Runtime): one slot per distinct observed minute, still in order.
    for (const minute of [...new Set(todays.map(bar => minuteOfDay(bar.datetime)))].sort((a, b) => a - b)) {
      slotIndexForMinute.set(minute, slots.length);
      slots.push({ time: TIMESHARE_TIME_BASE + slots.length * 60, clock: clockOf(minute) });
    }
  }
  if (!slots.length) return EMPTY_DATA;

  const closes = new Map<number, MarketKlineBar>();
  for (const bar of todays) {
    const index = slotIndexForMinute.get(minuteOfDay(bar.datetime));
    if (index !== undefined && index < slots.length) closes.set(index, bar);
  }
  // Minutes beyond the newest traded one are 未到分钟: whitespace on both lines. A skipped minute
  // before that (an illiquid pause) keeps the running 均价 and gaps only the price line.
  const lastTradedIndex = Math.max(...closes.keys());

  const price: TimesharePoint[] = [];
  const average: TimesharePoint[] = [];
  const volume: TimeshareVolume[] = [];
  let priceVolumeSum = 0;
  let volumeSum = 0;
  let averageValue: number | undefined;
  let latest: TimeshareReadout | null = null;
  for (const [index, slot] of slots.entries()) {
    const bar = closes.get(index);
    if (bar) {
      price.push({ time: slot.time, value: bar.close });
      priceVolumeSum += bar.close * bar.volume;
      volumeSum += bar.volume;
      if (volumeSum > 0) averageValue = priceVolumeSum / volumeSum;
      if (averageValue !== undefined) average.push({ time: slot.time, value: averageValue });
      else average.push({ time: slot.time });
      if (bar.volume > 0) volume.push({ time: slot.time, value: bar.volume, up: bar.close >= bar.open });
      latest = { time: slot.time, clock: slot.clock, price: bar.close, average: averageValue, volume: bar.volume, up: bar.close >= bar.open };
    } else {
      price.push({ time: slot.time });
      average.push(index < lastTradedIndex && averageValue !== undefined ? { time: slot.time, value: averageValue } : { time: slot.time });
    }
  }
  return { tradingDay: day, slots, price, average, volume, latest };
}

import type { RoundTrip, UnlinkedNote } from "./hooks/useTradeReview";

/** A round trip from `/api/account/pnl`: account-wide, with its closing trading day. */
export type PnlRound = RoundTrip & { approximate: boolean; tradingDay: string };
/** 按打法、时段、星期 (打法本第 2 批后半, 2026-10-25): one row per group with closed trades. */
export type BreakdownRow = { label: string; trades: number; wins: number; winRate: number; net: number; average: number };
export type ReviewBreakdown = { byPlaybook: BreakdownRow[]; byTimeBlock: BreakdownRow[]; byWeekday: BreakdownRow[] };

export type AccountPnlData = {
  /** Older Runtimes omit it. */
  breakdown?: ReviewBreakdown | null;
  initialBalance: number; equity: number | null; totalPnl: number | null; today: number | null; thisMonth: number;
  tradeCount: number; winRate: number | null; realizedNet: number; costSource: string; dataStatus: string;
  skippedSymbols: string[];
  /** Older Runtimes omit these. */
  accountName?: string | null; cumulativeCommission?: number | null; commissionSince?: string | null; tradingDay?: string | null;
  /** The ledger's currency (本站模拟合成一个账户, 2026-10-21): USD for the 外盘 sub-account, whose 本金 is its 10 万美元. */
  currency?: "CNY" | "USD";
  months: { month: string; realizedNet: number; commission: number | null; tradeCount: number; winRate: number | null }[];
  days: { day: string; realizedNet: number; commission: number | null; tradeCount: number; winRate: number | null; equity: number | null }[];
  trades: PnlRound[];
  /** 权益曲线 and the count behind 本月平仓 (2026-10-03); older Runtimes omit both. */
  equityCurve?: EquityPoint[]; thisMonthTradeCount?: number;
  /** 未关联的补充 (2026-10-04); older Runtimes omit it. */
  unlinkedNotes?: UnlinkedNote[];
};
/** One stored trading day: its equity and the day's net P&L (null when nothing witnesses it). */
export type EquityPoint = { day: string; equity: number; pnl: number | null };
export type PnlBucket = { key: string; realizedNet: number; tradeCount: number; wins: number; approximate: boolean; trades: PnlRound[] };
export type PnlMonth = PnlBucket & { days: PnlBucket[] };

export const realized = (trade: RoundTrip) => trade.net ?? trade.gross;

function bucket(key: string, trades: PnlRound[]): PnlBucket {
  return { key, trades, realizedNet: Math.round(trades.reduce((sum, t) => sum + realized(t), 0) * 100) / 100,
    tradeCount: trades.length, wins: trades.filter(t => realized(t) > 0).length, approximate: trades.some(t => t.approximate) };
}

/** Months (newest first) → trading days (newest first) → round trips (latest close first). */
export function groupRounds(trades: PnlRound[]): PnlMonth[] {
  const sorted = [...trades].sort((a, b) => b.closeTime - a.closeTime || a.id.localeCompare(b.id));
  const months = new Map<string, Map<string, PnlRound[]>>();
  for (const trade of sorted) {
    const days = months.get(trade.tradingDay.slice(0, 7)) ?? new Map<string, PnlRound[]>();
    months.set(trade.tradingDay.slice(0, 7), days);
    days.set(trade.tradingDay, [...(days.get(trade.tradingDay) ?? []), trade]);
  }
  return [...months.entries()].sort(([a], [b]) => b.localeCompare(a)).map(([month, days]) => {
    const dayBuckets = [...days.entries()].sort(([a], [b]) => b.localeCompare(a)).map(([day, items]) => bucket(day, items));
    return { ...bucket(month, dayBuckets.flatMap(d => d.trades)), days: dayBuckets };
  });
}

import { useEffect, useState } from "react";
import { requestJson } from "../api";
import type { ReviewTag } from "../dailyReview";
import { useDisclosureSurface } from "../uiSurfaces";
import { SectionCard, SideBySide, StatTiles } from "./Blocks";
import { TagChip } from "./DailyReview";
import { formatSigned, signClass } from "./futures/support";
import "./HabitCompare.css";

type Side = { count: number; pnl: number };
type Pick = { id: string; tradingDay: string | null; symbol: string; direction: "long" | "short"; volume: number;
  tag: ReviewTag | null; pnl: number; reason: string };
export type HabitCompareData = {
  status: "ok" | "insufficient"; basis: "net" | "gross"; days: number; rules: string[];
  planned: Side | number; needed?: number; actual?: Side; gap?: number;
  split?: { tag: ReviewTag | null; label: string; count: number; pnl: number }[];
  holding?: { lateLosers: { count: number; extra: number } | null; earlyWinners: { count: number; missed: number } | null };
  picks?: Pick[];
};

const POLL_MS = 60_000;
const money = (value: number) => formatSigned(value, 0);
const plain = (value: number) => Math.round(Math.abs(value)).toLocaleString("zh-CN");

/** 习惯对照's data (GET /api/account/habits) for the ledger the panel shows; an older Runtime answers 404 and the
 *  block simply never appears. Re-read once a minute: it is a 90-day look, not a live figure. */
export function useHabitCompare({ baseUrl, accountId }: { baseUrl: string | undefined; accountId?: string | null }) {
  const key = `${baseUrl}|${accountId ?? ""}`;
  const [data, setData] = useState<{ key: string; value: HabitCompareData }>();
  useEffect(() => {
    if (!baseUrl || accountId === null) return;
    let cancelled = false; let timer: ReturnType<typeof setTimeout>;
    const read = async () => {
      try {
        const value = await requestJson<HabitCompareData>(`${baseUrl}/api/account/habits`, {
          headers: accountId ? { "X-AI-Trader-Account": accountId } : undefined });
        if (!cancelled && value && (value.status === "ok" || value.status === "insufficient")) setData({ key, value });
      } catch { /* an older Runtime or a passing failure: the block stays as it was */ }
      finally { if (!cancelled) timer = setTimeout(read, POLL_MS); }
    };
    void read();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
  return data?.key === key ? data.value : undefined;
}

/** The snapshot's `review.habits`: what the block shows, in numbers. */
export function habitState(data: HabitCompareData) {
  if (data.status !== "ok") return { status: data.status, planned: data.planned, needed: data.needed ?? null };
  return { status: data.status, actual: data.actual, planned: data.planned, gap: data.gap,
    picks: (data.picks ?? []).map(pick => pick.id) };
}

/**
 * 习惯对照 (2026-10-25, owner-approved mock-up): 你实际 against 只做计划内 over the last 90 days, the gap split by the
 * 计划外 trades' tag, two holding estimates kept apart, and the trades that move it most — a click selects the trade
 * in 平仓记录 (the panel's own `review: {select}`).
 */
export function HabitCompareBlock({ data, onSelect }: { data: HabitCompareData | undefined; onSelect: (id: string) => void }) {
  const [howOpen, setHowOpen] = useDisclosureSurface("habits-how", { available: data?.status === "ok" });
  if (!data) return null;
  const basis = data.basis === "gross" ? "毛收益" : "扣费后";
  const hint = "只做计划内：同一段时间只留开仓标为“计划内”的单，和你实际的全部平仓比。下面的“拿单习惯”是估算，不和相差的钱相加。";
  if (data.status !== "ok" || !data.actual || typeof data.planned === "number") {
    return <div className="habit-compare-wrap" data-agent-target="review:habits">
      <SectionCard title="习惯对照" className="habit-compare" testId="habit-compare" hint={hint} note={`近 ${data.days} 天`}>
        <p className="section-card-body habit-compare-empty">计划内的单还不到 {data.needed ?? 5} 笔（现在 {typeof data.planned === "number" ? data.planned : data.planned.count} 笔），攒够了再对照</p>
      </SectionCard>
    </div>;
  }
  const { actual, planned, gap = 0, split = [], holding, picks = [] } = data;
  const late = holding?.lateLosers, early = holding?.earlyWinners;
  return <div className="habit-compare-wrap" data-agent-target="review:habits"><SectionCard title="习惯对照" className="habit-compare"
    testId="habit-compare" hint={hint} note={`近 ${data.days} 天 · ${basis} · ${actual.count} 笔平仓`}>
    <SideBySide>
      <div className="habit-compare-side"><small>你实际</small><b className={signClass(actual.pnl)}>{money(actual.pnl)}</b><small>{actual.count} 笔</small></div>
      <div className="habit-compare-side"><small>只做计划内</small><b className={signClass(planned.pnl)}>{money(planned.pnl)}</b>
        <small>{planned.count} 笔 · 相差 {plain(gap)}</small></div>
    </SideBySide>
    <div className="section-card-body habit-compare-body">
      {split.some(item => item.count) ? <>
        <p className="habit-compare-label">相差的 {plain(gap)} 来自计划外的单</p>
        <StatTiles columns={2} label="计划外的单" items={split.filter(item => item.count).map(item => ({
          label: `${item.label} ${item.count} 笔`,
          value: <span className={signClass(item.pnl)}>{money(item.pnl)}</span> }))} />
      </> : <p className="habit-compare-label">这段时间没有计划外的单</p>}
      {late || early ? <>
        <p className="habit-compare-label">拿单习惯 · 估算，不和上面相加</p>
        <StatTiles columns={2} label="拿单习惯" items={[
          ...(late ? [{ label: `亏单拿太久 ${late.count} 笔`, value: <span className={signClass(-late.extra)}>多亏约 {plain(late.extra)}</span> }] : []),
          ...(early ? [{ label: `赚单拿太短 ${early.count} 笔`, value: `少赚约 ${plain(early.missed)}` }] : []),
        ]} />
      </> : null}
      {picks.length ? <>
        <p className="habit-compare-label">最该看的几笔</p>
        <ul className="habit-compare-picks" aria-label="最该看的几笔">
          {picks.map(pick => <li key={pick.id}>
            <button type="button" onClick={() => onSelect(pick.id)}>
              <span>{pick.tradingDay?.slice(5) ?? ""}</span>
              <b>{pick.symbol.split(".").pop()} {pick.direction === "long" ? "多" : "空"} {pick.volume} 手</b>
              {pick.tag ? <TagChip tag={pick.tag} /> : null}
              <span className="habit-compare-pick-right"><span className={signClass(pick.pnl)}>{money(pick.pnl)}</span><small>{pick.reason}</small></span>
            </button>
          </li>)}
        </ul>
      </> : null}
      <details className="daily-review-how" open={howOpen} onToggle={event => setHowOpen((event.currentTarget as HTMLDetailsElement).open)}>
        <summary>怎么算的</summary>
        <ul>{data.rules.map(rule => <li key={rule}>{rule}</li>)}</ul>
      </details>
    </div>
  </SectionCard></div>;
}

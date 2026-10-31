import { ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import type { DailyReview, DailyReviewResponse, ReviewLeg, ReviewTag } from "../dailyReview";
import { TAG_LABELS } from "../dailyReview";
import { useDisclosureSurface } from "../uiSurfaces";
import { SectionCard, StatTiles } from "./Blocks";
import { formatSigned, signClass } from "./futures/support";
import "./DailyReview.css";

const money = (value: number | null | undefined) => value === null || value === undefined ? "—" : formatSigned(value, 0);
const clock = (value: string | null | undefined) => {
  const date = value ? new Date(value) : undefined;
  return date && !Number.isNaN(date.getTime())
    ? date.toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false, hour: "2-digit", minute: "2-digit" }) : "15:15";
};

export function TagChip({ tag, label }: { tag: ReviewTag; label?: string }) {
  return <span className="review-tag" data-tag={tag}>{label ?? TAG_LABELS[tag]}</span>;
}

/** 平仓记录 / 这笔交易: a round trip's 开 and 平 tags (one chip when both legs agree); nothing from an older Runtime. */
export function RoundTags({ open, close }: { open?: ReviewTag; close?: ReviewTag }) {
  if (!open || !close) return null;
  return <span className="review-round-tags">{open === close ? <TagChip tag={open} />
    : <><TagChip tag={open} label={`开 · ${TAG_LABELS[open]}`} /><TagChip tag={close} label={`平 · ${TAG_LABELS[close]}`} /></>}</span>;
}

/** “买开 螺纹钢 RB2701 × 1 @ 3,150”: a narrow line breaks between the contract and the quantity, never
 *  inside “× 1 @ 3,150” (a very narrow one still wraps inside the contract part, nothing overflows). */
function LegAction({ action }: { action: string }) {
  const at = action.lastIndexOf(" × ");
  if (at < 0) return <>{action}</>;
  return <>{action.slice(0, at)} <span className="daily-review-nowrap">{action.slice(at + 1)}</span></>;
}

/** 交易验尸 (2026-10-25): one 笔's checks, its entry judgment and the question answered into its 补充. */
function LegAutopsy({ leg, note, onAnswer }: {
  leg: ReviewLeg; note: string; onAnswer?: (roundId: string, text: string) => Promise<boolean>;
}) {
  const [draft, setDraft] = useState(note);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  useEffect(() => { setDraft(note); }, [note]);
  const checks = leg.checks ?? [];
  if (!checks.length && !leg.entryFit) return null;
  const { roundId, question } = leg;
  const save = async () => {
    if (!onAnswer || !roundId) return;
    setBusy(true); setStatus("");
    const saved = await onAnswer(roundId, draft.trim());
    setBusy(false); setStatus(saved ? "已记到这笔的补充" : "没有保存成功，请重试");
  };
  return <div className="daily-review-autopsy">
    {checks.length ? <ul aria-label="查出的问题">{checks.map(check => <li key={check.code}>
      <span className="daily-review-check">{check.label}</span>{check.text}</li>)}</ul> : null}
    {leg.entryFit?.fits ? <p className="daily-review-fit">合入场规则：{leg.entryFit.text}</p> : null}
    {question && roundId && onAnswer ? <div className="daily-review-question">
      <label><span>{question}</span>
        <textarea rows={2} maxLength={500} value={draft} placeholder="一句话就行"
          onChange={event => { setDraft(event.target.value); setStatus(""); }} /></label>
      <div className="trade-journal-actions">
        <button type="button" disabled={busy || draft.trim() === note.trim() || (!draft.trim() && !note)} onClick={() => void save()}>保存回答</button>
        {status ? <span role="status">{status}</span> : null}
      </div>
    </div> : null}
  </div>;
}

/** The four tiles as shown, also reported to the snapshot. */
export function dailyReviewTiles(review: DailyReview) {
  const { stats } = review;
  return [
    { label: "当日净盈亏", value: money(review.dayPnl),
      ...(review.dayPnlBasis === "live" ? { note: "结算前，按当时价格；结算后更正" } : {}) },
    { label: "成交", value: `${stats.legs} 笔` },
    { label: "按计划", value: `${stats.planned} / ${stats.legs}` },
    { label: "平仓胜率", value: stats.closes ? `${stats.wins} / ${stats.closes}` : "—" },
  ];
}

/**
 * 今日复盘 (2026-10-06, owner-approved mock-up): the day's review at the top of 账户复盘 (and of the phone's
 * 复盘摘要, two tiles a row). Not shown until a review exists — 15:15 on a trading day with fills — so the
 * page stays quiet before the close and on days without trades. ‹ › step through earlier reviews;
 * 重新生成 recomputes. 交易验尸 (2026-10-25): each 笔's checks and question (answered into its round trip's 补充
 * through `onQuestion`); on the week's last trading day 本周最该改的一件事 waits for 记住.
 */
export function DailyReviewBlock({ data, onDay, onRegenerate, onAnswer, busy, notice, compact = false, notes = {}, onQuestion }: {
  data: DailyReviewResponse | undefined;
  onDay: (day: string) => void;
  onRegenerate: () => void;
  onAnswer: (verdict: "remember" | "dismiss") => void;
  busy: string;
  notice: string;
  compact?: boolean;
  /** roundId → the saved 补充, shown in the answer box. */
  notes?: Record<string, string>;
  onQuestion?: (roundId: string, text: string) => Promise<boolean>;
}) {
  const review = data?.review ?? null;
  const [howOpen, setHowOpen] = useDisclosureSurface("review-how", { available: Boolean(review) });
  if (!review || !data) return null;
  const index = data.days.indexOf(review.tradingDay);
  const older = index >= 0 ? data.days[index + 1] : undefined;
  const newer = index > 0 ? data.days[index - 1] : undefined;
  const tiles = dailyReviewTiles(review);
  const observation = review.observation;
  return <SectionCard className="daily-review" label="今日复盘" testId="daily-review"
    title={<span className="daily-review-title">
      <button type="button" className="daily-review-step" aria-label="前一个交易日的复盘" title="前一个交易日" disabled={!older}
        onClick={() => older && onDay(older)}><ChevronLeft size={15} aria-hidden="true" /></button>
      今日复盘 · {review.tradingDay.slice(5)}
      <button type="button" className="daily-review-step" aria-label="后一个交易日的复盘" title="后一个交易日" disabled={!newer}
        onClick={() => newer && onDay(newer)}><ChevronRight size={15} aria-hidden="true" /></button>
    </span>}
    note={`${clock(review.generatedAt)} 生成 · ${review.stats.legs} 笔成交`}
    actions={<button type="button" className="daily-review-regenerate" disabled={Boolean(busy)} onClick={onRegenerate}>
      <RefreshCw size={13} aria-hidden="true" />{busy === "regenerate" ? "生成中" : "重新生成"}</button>}>
    <StatTiles columns={compact ? 2 : 4} label="今日复盘四格" items={tiles.map((tile, at) => ({
      ...tile, value: at === 0 ? <span className={signClass(review.dayPnl ?? 0)}>{tile.value}</span> : tile.value }))} />
    <div className="section-card-body daily-review-body">
      <p className="daily-review-summary">{review.summary.text}<small>{review.summary.basis}</small></p>
      <ol className="daily-review-legs" aria-label="今天的每一笔">
        {review.legs.map(leg => <li key={leg.id} className="daily-review-leg">
          <time>{leg.clock}</time>
          <div><b><LegAction action={leg.action} /></b><span>{leg.reason}{leg.sourceLabel && leg.source !== "strategy" ? ` · ${leg.sourceLabel}` : ""}{leg.approximate ? " · 估" : ""}</span></div>
          <div className="daily-review-leg-right"><TagChip tag={leg.tag} label={leg.tagLabel} />
            {leg.pnl !== null ? <span className={signClass(leg.pnl)}>{money(leg.pnl)}</span> : null}</div>
          <LegAutopsy key={leg.roundId ?? leg.id} leg={leg} note={(leg.roundId && notes[leg.roundId]) || ""} onAnswer={onQuestion} />
        </li>)}
      </ol>
      {observation ? <div className="daily-review-observation" data-status={observation.status} role="group" aria-label="本周最该改的一件事">
        {observation.status === "pending" ? <>
          <p><small>本周最该改的一件事</small>{observation.text}
            {observation.count ? <small>本周 {observation.count} 笔{observation.label ? `「${observation.label}」` : ""}</small> : null}</p>
          <div className="daily-review-observation-actions">
            <button type="button" className="strategy-primary" disabled={Boolean(busy)} onClick={() => onAnswer("remember")}>记住</button>
          </div>
        </> : observation.status === "remembered"
          ? <p role="status">记住了：{observation.text}<small>在 设置 → 记忆 里能查、能改、能撤销</small></p>
          : <p role="status" className="daily-review-muted">好，这一条不记。</p>}
      </div> : null}
      {notice ? <p className="daily-review-muted" role="status">{notice}</p> : null}
      <details className="daily-review-how" open={howOpen} onToggle={event => setHowOpen((event.currentTarget as HTMLDetailsElement).open)}>
        <summary>怎么算的</summary>
        <ul>{(review.rules.length ? review.rules : data.rules).map(rule => <li key={rule}>{rule}</li>)}</ul>
      </details>
    </div>
  </SectionCard>;
}

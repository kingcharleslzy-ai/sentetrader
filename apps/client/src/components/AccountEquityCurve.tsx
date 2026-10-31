import { useState, type PointerEvent } from "react";
import type { EquityPoint } from "../accountPnl";
import { formatNumber, formatSigned, signClass } from "./futures/support";

export const dayLabel = (day: string) => `${Number(day.slice(5, 7))} 月 ${Number(day.slice(8, 10))} 日`;

/** What the review snapshot reports about the curve (null without any stored day). */
export function equityCurveSummary(points: EquityPoint[], principal: number) {
  if (!points.length) return null;
  const values = points.map(point => point.equity);
  return { from: points[0].day, to: points.at(-1)!.day, days: points.length, startEquity: values[0], endEquity: values.at(-1)!,
    high: Math.max(...values), low: Math.min(...values), principal };
}

/**
 * 权益曲线 (2026-10-03): the account's equity per trading day from the daily snapshots, the
 * principal as a dashed baseline. Hover or tap picks a day; the readout line above the plot
 * shows it (the latest day by default), so nothing floats outside a narrow panel.
 */
export function AccountEquityCurve({ points, principal }: { points: EquityPoint[]; principal: number }) {
  const [active, setActive] = useState<number>();
  if (points.length < 2) return <p className="account-equity-empty">至少要两个交易日的日结才有曲线</p>;
  const values = points.map(point => point.equity);
  const high = Math.max(...values), low = Math.min(...values);
  // The principal stays in range so the baseline is always drawn; 8 % headroom on both ends.
  const top = Math.max(high, principal), bottom = Math.min(low, principal);
  const pad = (top - bottom || Math.abs(top) * 0.01 || 1) * 0.08;
  const y = (value: number) => (top + pad - value) / (top - bottom + 2 * pad) * 100;
  const x = (index: number) => index / (points.length - 1) * 100;
  const line = values.map((value, index) => `${(x(index) * 10).toFixed(1)},${y(value).toFixed(2)}`).join(" ");
  // A pick from a longer earlier curve falls back to the latest day.
  const shown = active !== undefined && active < points.length ? active : points.length - 1;
  const point = points[shown];
  const pick = (event: PointerEvent<HTMLDivElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    if (!box.width || !Number.isFinite(event.clientX)) return;
    const fraction = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
    setActive(Math.round(fraction * (points.length - 1)));
  };
  return <div className="account-equity-chart">
    <p className="account-equity-readout" aria-live="polite">
      <span>{dayLabel(point.day)}</span>
      <span>权益 <strong>{formatNumber(point.equity, 0)}</strong></span>
      <span>当日 <strong className={signClass(point.pnl)}>{point.pnl === null ? "—" : formatSigned(point.pnl, 0)}</strong></span>
    </p>
    <div className="account-equity-plot" onPointerMove={pick} onPointerDown={pick} onPointerLeave={event => { if (event.pointerType === "mouse") setActive(undefined); }}>
      <svg viewBox="0 0 1000 100" preserveAspectRatio="none" role="img"
        aria-label={`权益曲线：${dayLabel(points[0].day)} ${formatNumber(values[0], 0)} → ${dayLabel(points.at(-1)!.day)} ${formatNumber(values.at(-1)!, 0)}，最高 ${formatNumber(high, 0)}，最低 ${formatNumber(low, 0)}，本金 ${formatNumber(principal, 0)}`}>
        <polygon className="account-equity-area" points={`0,100 ${line} 1000,100`} />
        <line className="account-equity-principal" x1="0" x2="1000" y1={y(principal)} y2={y(principal)} />
        {active !== undefined && <line className="account-equity-cursor" x1={x(shown) * 10} x2={x(shown) * 10} y1="0" y2="100" />}
        <polyline className="account-equity-line" points={line} />
      </svg>
      <span className="account-equity-dot" style={{ left: `${x(shown)}%`, top: `${y(point.equity)}%` }} />
      <span className="account-equity-principal-label" style={{ top: `${y(principal)}%` }}>本金 {formatNumber(principal, 0)}</span>
    </div>
    <div className="account-equity-axis"><span>{dayLabel(points[0].day)}</span>
      <span><span>最高 {formatNumber(high, 0)}</span><span>最低 {formatNumber(low, 0)}</span></span><span>{dayLabel(points.at(-1)!.day)}</span></div>
  </div>;
}

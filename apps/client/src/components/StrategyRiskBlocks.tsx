import type { Amount, StrategyDrawdown, StrategyFunds } from "../api";
import { HelpTip, StatTiles } from "./Blocks";
import { formatNumber } from "./futures/support";
import "./StrategyRiskBlocks.css";

/**
 * 按资金比例定手数与回撤停机 (2026-10-09, the owner approved risk-controls-mockup.html): the two lines of the funds
 * calculation and the 回撤停机 status, shared by the editor's ④, the desktop 正在运行 card and the phone's run card.
 * Every figure is the Runtime's own (`funds` / `drawdown` of the monitor, or the editor's preview estimate); nothing
 * here works a figure out, and a missing one is left out or its reason shown — never a made-up 0.
 */
export const RISK_HELP = {
  drawdown: "只看这条策略自己的权益：上线时的账户权益作为起点，只加上本策略扣费后的已实现盈亏和持仓盈亏，记下最高值。别的策略、手工交易和出入金都不改变这条线。"
    + "回撤 =（最高 − 当前）÷ 最高，从上线起算，不是当天亏了多少。例：起点 100 万，最高到 105 万，设 5%，跌到 99.75 万就触发。"
    + "触发后先停新开仓、撤本策略挂单、平本策略持仓；能否成交受行情和涨跌停限制，触发不等于已经平完。之后一直停着，要你手动恢复。",
  resume: "恢复运行以现在的权益作为新的起点和最高点重新计算回撤；只有本策略的挂单和持仓都处理完了才能恢复。",
} as const;

/** A Runtime amount as a number (null when absent or not a number). */
export function amount(value: Amount): number | null {
  if (value == null || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}
/** 100.00 万 / 8,000 元: the mockup's money words; a foreign (美元) strategy says 1.20 万美元 / 8,000 美元. */
export function moneyText(value: number, unit = "元"): string {
  return Math.abs(value) >= 10_000 ? `${(value / 10_000).toFixed(2)} 万${unit === "元" ? "" : unit}` : `${formatNumber(value, 0)} ${unit}`;
}
const percentText = (value: number) => `${Number(value.toFixed(2))}%`;
const clock = (time: number) => new Date(time).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
/** The basis of 当前 when it is not the fills' own fees (扣费后 is the help's rule). */
const FEE_BASIS = { estimated: "手续费按费率估算", unavailable: "手续费暂缺" } as const;

/**
 * 剩余额度 = 权益 × 比例 − 本策略已占（含挂单） / 剩余额度 ÷ 每手保证金 → 本次最多 N 手 (先减后除). A line whose figures
 * are not all there is left out; `unavailableReason` (行情、账户或保证金缺失 / 过期) replaces both.
 */
export function FundsLines({ funds, loading, error, at, unit }: { funds?: StrategyFunds | null; loading?: boolean; error?: string; unit?: string;
  /** The editor's estimate: when the Runtime worked it out (the monitor's own figures refresh with the card). */ at?: number }) {
  if (error) return <p className="risk-warn" role="status">{error}</p>;
  if (!funds) return loading ? <p className="risk-hint" role="status">正在按当前账户计算…</p> : null;
  if (funds.unavailableReason) return <p className="risk-warn" role="status" data-testid="funds-unavailable">{funds.unavailableReason}</p>;
  const equity = amount(funds.equity), percent = amount(funds.equityPercent), used = amount(funds.usedMargin), pending = amount(funds.pendingMargin);
  const left = amount(funds.remainingBudget), perLot = amount(funds.marginPerLot), lots = funds.availableLots;
  // 已占 only when both parts are known: a missing one is not a 0.
  const taken = used != null && pending != null ? used + pending : null;
  const short = lots != null && lots < 1;
  const money = (value: number) => moneyText(value, unit);
  return <div className="risk-funds" data-zero={short || undefined} aria-live="polite" aria-label="按资金比例的额度">
    {equity != null && percent != null && taken != null && left != null ? <p>
      <span className="risk-hint">按当前账户{at ? ` · ${clock(at)} 估算` : ""}</span>
      <span>剩余额度 = 权益 <b>{money(equity)}</b> × <b>{percentText(percent)}</b> − 本策略已占 <b>{money(taken)}</b>
        {pending ? <>（含挂单 <b>{money(pending)}</b>）</> : null} = <b>{money(left)}</b></span>
    </p> : null}
    {left != null && perLot != null && lots != null ? <p>
      <span>剩余额度 <b>{money(left)}</b> ÷ 每手保证金 <b>{money(perLot)}</b> → {short ? <b className="risk-lots">不足 1 手，这次不开</b> : <>本次最多 <b className="risk-lots">{lots} 手</b></>}</span>
    </p> : null}
  </div>;
}

/** 权益曲线 (the editor's preview and 回撤停机's own): the start as a dashed line, and optional 最高 / 停机线 marks. */
export function EquityCurve({ points, marks = [], label, className = "se-curve" }: { points: { time: number; equity: number }[];
  marks?: { value: number; tone: "peak" | "stop" }[]; label?: string; className?: string }) {
  if (points.length < 2) return null;
  const values = points.map(point => point.equity);
  const all = [...values, ...marks.map(mark => mark.value)];
  const low = Math.min(...all), high = Math.max(...all), span = high - low || 1;
  const width = 440, height = marks.length ? 96 : 72;
  const y = (value: number) => 6 + (1 - (value - low) / span) * (height - 12);
  const path = points.map((point, index) => `${(index / (points.length - 1) * width).toFixed(1)},${y(point.equity).toFixed(1)}`).join(" ");
  const up = values.at(-1)! >= values[0];
  return <svg className={className} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img"
    aria-label={label ?? `权益曲线，${up ? "期末高于期初" : "期末低于期初"}`}>
    {marks.length ? null : <line x1="0" x2={width} y1={y(values[0])} y2={y(values[0])} className="se-curve-base"/>}
    {marks.map(mark => <line key={mark.tone} x1="0" x2={width} y1={y(mark.value)} y2={y(mark.value)} className={`risk-curve-${mark.tone}`}/>)}
    <polyline points={path} fill="none" data-sign={up ? "up" : "down"} vectorEffect="non-scaling-stroke"/>
  </svg>;
}

const STATES = {
  running: { text: "运行中", tone: "accent" },
  liquidating: { text: "已停新开仓 · 平仓处理中", tone: "warning" },
  stopped: { text: "已停止 · 手动恢复", tone: "danger" },
  unavailable: { text: "回撤暂时算不出", tone: "warning" },
} as const;

/**
 * 回撤停机 on a running strategy: 起始 / 最高 / 当前 / 回撤 (four tiles), its own equity curve with the peak and the stop
 * line, and the state — 运行中 / 已停新开仓 · 平仓处理中 / 已停止 · 手动恢复 / 回撤暂时算不出 (恢复运行 is the one primary
 * action of the last two and only enabled when the Runtime says `canResume`). Nothing is drawn when it is not used (absent / inactive).
 */
export function DrawdownStatus({ drawdown, busy, columns = 4, buttonClass, onResume, unit }: { drawdown?: StrategyDrawdown | null; busy?: boolean;
  columns?: 2 | 4; unit?: string; buttonClass: string; onResume: () => void }) {
  if (!drawdown || drawdown.status === "inactive") return null;
  const start = amount(drawdown.initialEquity), peak = amount(drawdown.peakEquity), now = amount(drawdown.currentEquity);
  const fall = amount(drawdown.drawdownPercent), limit = amount(drawdown.limitPercent);
  const line = peak != null && limit != null ? peak * (1 - limit / 100) : null;
  const state = STATES[drawdown.status] ?? STATES.unavailable;
  // 已停止 / 暂时算不出: both offer 恢复运行 (a new round from the current equity), and the Runtime's reason it is
  // blocked comes before the trigger note.
  const resumable = drawdown.status === "stopped" || drawdown.status === "unavailable";
  const note = (resumable ? drawdown.resumeBlockedReason : null) || drawdown.reason;
  const points = (drawdown.curve ?? []).map(point => ({ time: Number(point.time), equity: amount(point.equity) }))
    .filter((point): point is { time: number; equity: number } => point.equity != null);
  const marks = [...(peak != null ? [{ value: peak, tone: "peak" as const }] : []), ...(line != null ? [{ value: line, tone: "stop" as const }] : [])];
  const money = (value: number) => moneyText(value, unit);
  const show = (value: number | null) => value == null ? "—" : money(value);
  return <section className="risk-dd" data-status={drawdown.status} aria-label="回撤停机" data-testid="strategy-drawdown">
    <header className="risk-dd-head"><b>回撤停机</b><HelpTip helpKey="strategy:drawdown" label="回撤停机">{RISK_HELP.drawdown}</HelpTip>
      {line != null ? <span className="risk-hint">停机线 {money(line)}</span> : null}
      {drawdown.feeSource === "estimated" || drawdown.feeSource === "unavailable" ? <span className="risk-hint" data-testid="drawdown-fee-basis">{FEE_BASIS[drawdown.feeSource]}</span> : null}</header>
    <StatTiles columns={columns} label="回撤停机" items={[
      { label: "起始权益", value: show(start) }, { label: "最高", value: show(peak) }, { label: "当前", value: show(now) },
      { label: limit != null ? `回撤（线 ${percentText(limit)}）` : "回撤", value: fall == null ? "—" : percentText(fall) }]}/>
    <EquityCurve points={points} marks={marks} className="risk-curve" label="本策略权益曲线（最高线与停机线）"/>
    <div className="risk-state">
      <span className="risk-badge" data-tone={state.tone}>{state.text}</span>
      {drawdown.status === "running" && now != null && line != null ? <span className="risk-hint">离停机线还有 {money(Math.max(0, now - line))}</span> : null}
      {note ? <span className={drawdown.status === "liquidating" || drawdown.status === "unavailable" ? "risk-warn" : "risk-hint"}>{note}</span> : null}
      {resumable ? <span className="risk-resume">
        <HelpTip helpKey="strategy:drawdown-resume" label="恢复运行">{RISK_HELP.resume}</HelpTip>
        <button type="button" className={buttonClass} disabled={busy || !drawdown.canResume} onClick={onResume}>恢复运行</button>
      </span> : null}
    </div>
  </section>;
}

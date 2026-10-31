import { ChartSpline, MapPin, RotateCcw } from "lucide-react";
import type { ReactNode } from "react";
import { backtestStatusLabel, EXIT_ORDER, type BacktestParams, type BacktestQueueState, type StrategyBacktestResult, type StrategyBacktestTrade } from "../strategyBacktests";
import { periodLabel, spreadWords } from "../strategyApi";
import { useDisclosureSurface } from "../uiSurfaces";
import { SectionCard } from "./Blocks";
import { CardShell } from "./CardShell";
import { formatNumber, formatSigned } from "./futures/support";
import "./StrategyBacktestView.css";

const time = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const clock = (ns: number) => time.format(new Date(ns / 1e6));
const tone = (value: number | null | undefined) => value == null || value === 0 ? "" : value > 0 ? "positive" : "negative";
const percent = (value: number | null | undefined) => value == null ? "—" : `${formatNumber(value, 1)}%`;
/** Core figures stay on one line in a narrow panel: 10 万 and above read as “148.5万”. */
const compact = (value: number, signed: boolean) => Math.abs(value) >= 100_000
  ? `${signed && value > 0 ? "+" : ""}${formatNumber(value / 10_000, 1)}万` : signed ? formatSigned(value, 0) : formatNumber(value, 0);

/** 资金曲线: a light SVG line scaled to its own range — the theme's accent, or (结果页, 2026-10-08) the
 *  market's up / down colour for a run that made / lost money. */
export function EquityLine({ points, tone }: { points: { equity: number }[]; tone?: "up" | "down" }) {
  if (points.length < 2) return null;
  const values = points.map(point => point.equity);
  const low = Math.min(...values), high = Math.max(...values), range = high - low || 1;
  const line = values.map((value, index) => `${(index / (values.length - 1) * 300).toFixed(1)},${(56 - (value - low) / range * 52).toFixed(1)}`).join(" ");
  return <svg className="strategy-backtest-curve" viewBox="0 0 300 60" preserveAspectRatio="none" role="img" aria-label={`资金曲线：${formatNumber(values[0])} → ${formatNumber(values.at(-1))}`}>
    <polyline points={line} fill="none" stroke={tone === "up" ? "var(--market-up)" : tone === "down" ? "var(--market-down)" : "var(--accent)"}
      strokeWidth={tone ? 2 : 1.6} vectorEffect="non-scaling-stroke" strokeLinejoin="round"/>
  </svg>;
}

/** 价差小图 (2026-09-29): the spread's own close per judged bar, same style as the equity line,
 * with each trade's 开仓/平仓 points on it — a buy point red-up, a sell point green-down (the same
 * 买卖点 colors as the contract chart; the marks are part of the SVG, not the chart's overlay). */
function SpreadLine({ points, trades }: { points: { time: number; value: number }[]; trades?: StrategyBacktestTrade[] }) {
  if (points.length < 2) return null;
  const values = points.map(point => point.value);
  const low = Math.min(...values), high = Math.max(...values), range = high - low || 1;
  const first = points[0].time, last = points.at(-1)!.time, span = last - first || 1;
  const x = (time: number) => (time - first) / span * 300;
  const y = (value: number) => 56 - (value - low) / range * 52;
  const line = values.map((value, index) => `${(index / (values.length - 1) * 300).toFixed(1)},${y(value).toFixed(1)}`).join(" ");
  const marks = (trades ?? []).flatMap(trade => {
    const openBuy = trade.direction === "long";   // 做多价差 opens buying the spread, 做空 selling it
    return [
      { time: trade.openTime, value: trade.openPrice, buy: openBuy, label: `开仓 ${formatNumber(trade.openPrice, 2)}` },
      { time: trade.closeTime, value: trade.closePrice, buy: !openBuy, label: `平仓 ${formatNumber(trade.closePrice, 2)}` },
    ];
  }).filter(mark => mark.time >= first && mark.time <= last && Number.isFinite(mark.value));
  return <svg className="strategy-backtest-curve" viewBox="0 0 300 60" preserveAspectRatio="none" role="img" aria-label={`价差：${formatNumber(values[0], 2)} → ${formatNumber(values.at(-1)!, 2)}`}>
    <polyline points={line} fill="none" stroke="var(--accent)" strokeWidth="1.6" vectorEffect="non-scaling-stroke" strokeLinejoin="round"/>
    {marks.map((mark, index) => <polygon key={index}
      points={mark.buy ? `${x(mark.time).toFixed(1)},${(y(mark.value) - 3).toFixed(1)} ${(x(mark.time) - 3).toFixed(1)},${(y(mark.value) + 2).toFixed(1)} ${(x(mark.time) + 3).toFixed(1)},${(y(mark.value) + 2).toFixed(1)}`
        : `${(x(mark.time) - 3).toFixed(1)},${(y(mark.value) - 2).toFixed(1)} ${(x(mark.time) + 3).toFixed(1)},${(y(mark.value) - 2).toFixed(1)} ${x(mark.time).toFixed(1)},${(y(mark.value) + 3).toFixed(1)}`}
      fill={mark.buy ? "var(--market-up)" : "var(--market-down)"} vectorEffect="non-scaling-stroke">
      <title>{mark.label}</title>
    </polygon>)}
  </svg>;
}

function TradeRow({ trade }: { trade: StrategyBacktestTrade }) {
  return <li><span>#{trade.id} {trade.direction === "long" ? "多" : "空"} {trade.volume} {trade.legs?.length ? "份价差" : "手"}{trade.adds ? `（含加仓 ${trade.adds} 次）` : ""}</span>
    <span>{clock(trade.openTime)} {formatNumber(trade.openPrice, 2)} → {clock(trade.closeTime)} {formatNumber(trade.closePrice, 2)}</span>
    <span>{trade.exitLabel} · {trade.barsHeld} 根</span>
    <b className={tone(trade.net)}>{formatSigned(trade.net, 0)}</b>
    {trade.legs?.length ? <details className="strategy-backtest-legs"><summary>详情</summary>
      <ul>{trade.legs.map(leg => <li key={leg.symbol}>
        <span>{leg.symbol} · {leg.direction === "buy" ? "买" : "卖"} {leg.volume} 手</span>
        <span>{clock(leg.openTime)} {formatNumber(leg.openPrice, 2)} → {clock(leg.closeTime)} {formatNumber(leg.closePrice, 2)}</span>
        <span>毛利 {formatSigned(leg.gross, 0)} · 手续费 {formatNumber(leg.fees, 0)}</span>
      </li>)}</ul>
    </details> : null}</li>;
}

/** The figures of one 规则回测: one row of core numbers, exits by reason, the equity line and the folded trade list. */
export function BacktestResultBody({ result, tradesOpen, onTradesToggle }: { result: StrategyBacktestResult; tradesOpen: boolean; onTradesToggle: (open: boolean) => void }) {
  const { summary, coverage } = result;
  const reasons = [...result.exitReasons].sort((a, b) => EXIT_ORDER.indexOf(a.reason) - EXIT_ORDER.indexOf(b.reason));
  const total = result.tradesTotal ?? summary.trades;
  return <div className="strategy-backtest-body">
    {/* 回测与体检: a run that is not stuck still says so (a stuck one shows the warning block instead). */}
    {result.checkLine && !result.headline && <p className="strategy-backtest-check" role="note">{result.checkLine}</p>}
    <dl className="strategy-backtest-metrics">
      <div><dt>净收益</dt><dd className={tone(summary.netProfit)} title={formatSigned(summary.netProfit, 2)}>{compact(summary.netProfit, true)}</dd></div>
      <div><dt>最大回撤</dt><dd title={formatNumber(summary.maxDrawdown, 2)}>{compact(summary.maxDrawdown, false)}<small>{percent(summary.maxDrawdownPercent)}</small></dd></div>
      <div><dt>胜率</dt><dd>{percent(summary.winRate)}</dd></div>
      <div><dt>盈亏比</dt><dd>{summary.payoffRatio == null ? "—" : formatNumber(summary.payoffRatio, 2)}</dd></div>
      <div><dt>次数</dt><dd>{summary.trades}</dd></div>
    </dl>
    {reasons.length > 0 && <ul className="strategy-backtest-reasons" aria-label="出场原因">
      {reasons.map(item => <li key={item.reason}><span>{item.label}</span><b>{item.count} 笔</b><em className={tone(item.net)}>{formatSigned(item.net, 0)}</em></li>)}
    </ul>}
    {/* 价差: two curves need their names — the equity line and the spread's own 走势. */}
    {result.spread ? <p className="strategy-backtest-curve-label">资金曲线</p> : null}
    <EquityLine points={result.equityCurve}/>
    {result.spreadCurve?.length ? <>
      <p className="strategy-backtest-curve-label">价差走势</p>
      <SpreadLine points={result.spreadCurve} trades={result.trades}/>
    </> : null}
    <p className="strategy-backtest-coverage">{coverage.firstTradingDay ?? "—"} 至 {coverage.lastTradingDay ?? "—"} · {coverage.tradingDays} 个交易日 · {formatNumber(coverage.bars)} 根K线
      {summary.averageBarsHeld != null ? ` · 平均持仓 ${formatNumber(summary.averageBarsHeld, 1)} 根` : ""} · 手续费 {formatNumber(summary.fees, 0)}</p>
    {result.feeEstimate && <p className={`strategy-backtest-fee${result.feeEstimate.warning ? " warning" : ""}`} role="note">{result.feeEstimate.text}</p>}
    {(result.notes ?? []).map(note => <p key={note} className="strategy-backtest-note" role="note">{note}</p>)}
    {total > 0 && <details className="strategy-backtest-trades" open={tradesOpen} onToggle={event => { const open = event.currentTarget.open; if (open !== tradesOpen) onTradesToggle(open); }}>
      <summary>逐笔清单（{total} 笔{result.trades.length < total ? `，显示最近 ${result.trades.length} 笔` : ""}）</summary>
      <ol>{[...result.trades].reverse().map(trade => <TradeRow key={trade.id} trade={trade}/>)}</ol>
    </details>}
  </div>;
}

export function backtestTitle(result: Pick<StrategyBacktestResult, "symbol" | "timeframeSeconds" | "spread">) {
  const subject = result.spread ? `${spreadWords(result.spread)} 价差` : result.symbol;
  return `规则回测 · ${subject} · ${periodLabel(result.timeframeSeconds)}`;
}

/**
 * 详情里的回测结果卡: the parameter row (区间 · 滑点) with 重跑, the result and 在图上标出买卖点.
 * Commandable as `strategyBacktest` (the workbench registers it with the card's state).
 */
export function StrategyBacktestPanel({ params, running, queue, result, error, tradesOpen, marked, markNote, onParams, onRun, onMark, onTradesToggle }: {
  params: BacktestParams; running: boolean; queue?: BacktestQueueState; result?: StrategyBacktestResult; error?: string; tradesOpen: boolean;
  marked: boolean; markNote?: ReactNode;
  onParams: (params: Partial<BacktestParams>) => void; onRun: () => void; onMark: (mark: boolean) => void; onTradesToggle: (open: boolean) => void;
}) {
  return <CardShell icon={<ChartSpline/>} title={result ? backtestTitle(result) : "规则回测"} testId="strategy-backtest-card" className="strategy-backtest-card"
    badge={backtestStatusLabel({ running, queue }) ?? (error ? "未完成" : undefined)} badgeTone={error && !running ? "warning" : "neutral"}
    details={result ? <ul className="strategy-backtest-assumptions">{result.assumptions.map(line => <li key={line}>{line}</li>)}</ul> : undefined}
    actions={result ? <>
      {/* 价差: a trade's prices are spread values — no mark on a contract's price chart (the `strategyBacktest`
          command says the same); the legs' own fills are in the trade list's 详情. */}
      {!result.spread ? <button type="button" disabled={running} aria-pressed={marked} onClick={() => onMark(!marked)}><MapPin size={14}/>{marked ? "清除买卖点" : "在图上标出买卖点"}</button> : null}
    </> : undefined}>
    <form className="strategy-backtest-params" data-agent-target="strategy-backtest-params" onSubmit={event => { event.preventDefault(); onRun(); }}>
      <label>区间 · 交易日<input type="number" min="1" max="2000" placeholder="默认" value={params.tradingDays ?? ""} disabled={running}
        onChange={event => { const value = event.target.valueAsNumber; onParams({ tradingDays: Number.isFinite(value) && value >= 1 ? Math.min(2000, Math.floor(value)) : null }); }}/></label>
      <label>滑点<select aria-label="滑点" value={params.slippageTicks} disabled={running} onChange={event => onParams({ slippageTicks: Number(event.target.value) })}>
        <option value={0}>0 跳</option><option value={1}>1 跳</option><option value={2}>2 跳</option></select></label>
      <button type="submit" disabled={running}><RotateCcw size={14}/>{running ? queue ? "排队中…" : "回测中…" : result ? "重跑" : "开始回测"}</button>
    </form>
    {error && <p className="strategy-backtest-error" role="alert">{error}</p>}
    {markNote && <p className="strategy-backtest-note" role="status">{markNote}</p>}
    {result && <BacktestResultBody result={result} tradesOpen={tradesOpen} onTradesToggle={onTradesToggle}/>}
  </CardShell>;
}

/** Conversation card for a 规则回测 (from the AI tool or the 详情 AI menu), marked as a rule backtest. */
export function StrategyBacktestConversationCard({ result, id }: { result: StrategyBacktestResult; id: string }) {
  const [tradesOpen, setTradesOpen] = useDisclosureSurface("strategy-backtest-trades", { id });
  return <CardShell icon={<ChartSpline/>} title={`${result.strategyName} · ${backtestTitle(result)}`} id={id} testId="strategy-backtest-result"
    badge="规则回测" badgeTone="positive"
    details={<ul className="strategy-backtest-assumptions">{result.assumptions.map(line => <li key={line}>{line}</li>)}</ul>}>
    {result.headline && <SectionCard tone="warning" title="体检：这条策略几乎不会开仓" label="体检" testId="strategy-backtest-health">
      <div className="section-card-body strategy-health">
        <p>{result.headline}</p>
        {(result.advice?.length ?? 0) > 0 && <ol className="strategy-health-advice" aria-label="体检建议">
          {result.advice!.map(item => <li key={item.id}><span>{item.text}</span></li>)}</ol>}
      </div>
    </SectionCard>}
    <BacktestResultBody result={result} tradesOpen={tradesOpen} onTradesToggle={setTradesOpen}/>
  </CardShell>;
}

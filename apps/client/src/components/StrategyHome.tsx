import { useEffect, useRef, useState } from "react";
import { ArrowLeft, Send } from "lucide-react";
import { getStrategyMonitor, type StrategyMonitorSnapshot } from "../api";
import { strategyText, type PlanRow, type SavedStrategy } from "../strategyApi";
import type { StrategyBacktestResult } from "../strategyBacktests";
import type { CompareCard } from "../strategyCompare";
import { EquityLine } from "./StrategyBacktestView";
import { HelpTip } from "./Blocks";
import { ParamHelp } from "./ParamHelp";
import { formatNumber, formatSigned, signClass } from "./futures/support";
import "./StrategyHome.css";

/**
 * 策略页改版第八批 (2026-10-08; the owner approved the mockup): the pieces of the new 策略 page — one
 * card per strategy in 我的策略 (新建 / 编辑 open the 策略编辑卡, StrategyEditorCard), and the 结果页 (big
 * figures, the equity line, AI 怎么看, the four plain rule rows, 试几组参数比一比 and the three actions).
 * The workbench owns the data and the commands; these only draw and call back.
 */

/** “+1.2 万” on a card, whole yuan below ten thousand. */
export function cardMoney(value: number, signed = true): string {
  if (Math.abs(value) >= 10_000) return `${signed && value > 0 ? "+" : ""}${formatNumber(value / 10_000, 1)} 万`;
  return signed ? formatSigned(value, 0) : formatNumber(value, 0);
}
const percent = (value: number | null | undefined) => value == null ? "—" : `${Math.round(value)}%`;

/** The instance's monitor for a card, read every `ms` (运行中 / 已暂停 only). */
export function useInstanceMonitor(baseUrl: string, strategyId: string | undefined, ms = 10_000, refresh?: unknown) {
  const [monitor, setMonitor] = useState<StrategyMonitorSnapshot>();
  const [unavailable, setUnavailable] = useState(false);
  useEffect(() => {
    setMonitor(undefined);
    if (!strategyId) return;
    let cancelled = false; let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try { const state = await getStrategyMonitor(baseUrl, strategyId); if (!cancelled) { setMonitor(state); setUnavailable(false); } }
      catch { if (!cancelled) setUnavailable(true); }
      finally { if (!cancelled) timer = setTimeout(poll, ms); }
    };
    void poll(); return () => { cancelled = true; clearTimeout(timer); };
  }, [baseUrl, strategyId, ms, refresh]);
  return { monitor, unavailable };
}

export type CardState = { key: "running" | "paused" | "idle" | "draft"; label: string };

/** 今日判断偏空，这个策略今天在做多 — a yellow line; nothing when there is no clash. */
export function ClashLine({ clash }: { clash?: StrategyMonitorSnapshot["researchClash"] }) {
  if (!clash) return null;
  return <p className="sh-clash" role="note" data-testid="strategy-clash"><span aria-hidden="true">⚠</span><span>{clash.text}</span></p>;
}

/** One strategy in 我的策略: name, state, contract, its one sentence, the latest backtest and today. */
export function StrategyCard({ item, state, contract, monitor, backtesting, disabled, onOpen, onBacktest }: {
  item: SavedStrategy; state: CardState; contract: string; monitor?: StrategyMonitorSnapshot; backtesting: boolean; disabled: boolean;
  onOpen: () => void; onBacktest: () => void;
}) {
  const latest = item.latestBacktest;
  const signals = monitor?.signalsToday;
  return <article className="sh-card" data-state={state.key} data-testid="strategy-card">
    <button type="button" className="sh-card-open" disabled={disabled} aria-label={`${strategyText(item.name)} · ${state.label}`} onClick={onOpen}>
      <span className="sh-card-top"><b>{strategyText(item.name)}</b><em className="sh-state" data-state={state.key}>{state.label}</em><small className="sh-sym">{contract}</small></span>
      {item.planSummary ? <span className="sh-plain">{item.planSummary}</span> : null}
    </button>
    <div className="sh-score">
      {latest ? <>
        <span>最近回测 <b className={signClass(latest.netProfit)}>{cardMoney(latest.netProfit)}</b></span>
        <span>最多亏 <b className={latest.maxDrawdown ? "negative" : ""}>{latest.maxDrawdown ? `-${cardMoney(latest.maxDrawdown, false)}` : "0"}</b></span>
        <span>胜率 <b>{percent(latest.winRate)}</b></span>
      </> : <>
        <span>{backtesting ? "回测中…" : "还没回测"}</span>
        {!backtesting ? <button type="button" className="sh-btn" disabled={disabled} onClick={onBacktest}>回测</button> : null}
      </>}
      {state.key === "running" && typeof signals === "number" ? <span>今天 <b>{signals}</b> 个信号</span> : null}
    </div>
    <ClashLine clash={monitor?.researchClash}/>
  </article>;
}

/** A plain word with its professional name behind “?” (the same floating note as a parameter's). */
function TermPart({ part }: { part: PlanRow["parts"][number] }) {
  if (!part.term) return <>{part.text}</>;
  return <><b>{part.text}</b><ParamHelp paramKey={`term-${part.term}`} label={part.text}
    help={{ what: part.help ?? part.term, up: "", down: "" }}/></>;
}

/** 开仓 / 平仓 / 止损 / 风险 — one plain row each. */
export function RuleRows({ rows }: { rows: PlanRow[] }) {
  return <div className="sh-rules" aria-label="规则" data-agent-target="strategy-rules">
    {rows.map(row => <div key={row.key} className="sh-rule"><span>{row.label}</span>
      <div>{row.parts.map((part, index) => <TermPart key={index} part={part}/>)}</div></div>)}
  </div>;
}

/** 过去 6 个月按这个做 — the range and the basis in one line. */
function resultHead(result: StrategyBacktestResult, lots: number) {
  const days = result.coverage.tradingDays;
  const span = days >= 100 ? "6 个月" : days >= 40 ? `${Math.round(days / 20)} 个月` : `${days} 个交易日`;
  const fees = result.feeEstimate?.source === "none" ? "未扣手续费" : "已扣手续费";
  const range = `${result.coverage.firstTradingDay ?? "—"} 到 ${(result.coverage.lastTradingDay ?? "—").slice(5)}`;
  // Fewer days than asked for: the period's history holds no more (1 分钟 K 线只有最近约一个月).
  const short = result.coverage.limited || (result.coverage.requestedTradingDays ?? 0) > days ? "（这个周期的历史只取得到这么多）" : "";
  return { title: `过去 ${span}按这个做`, hint: `每次 ${lots} 手，${fees} · ${range}${short}` };
}

/** 一共赚了 / 最多一次亏到 / 100 次里赢: the three figures in large type. */
export function BigNumbers({ result }: { result: StrategyBacktestResult }) {
  const { summary } = result;
  const wins = summary.winRate == null ? null : Math.round(summary.winRate);
  return <dl className="sh-big" aria-label="回测结果">
    <div><dt>{summary.netProfit >= 0 ? "一共赚了" : "一共亏了"}</dt><dd><b className={signClass(summary.netProfit)}>{formatSigned(summary.netProfit, 0)}</b>
      <small>{summary.returnPercent == null ? " " : `收益 ${formatNumber(summary.returnPercent, summary.returnPercent && Math.abs(summary.returnPercent) < 1 ? 2 : 0)}%`}</small></dd></div>
    <div><dt>最多一次亏到</dt><dd><b className={summary.maxDrawdown ? "negative" : ""}>{summary.maxDrawdown ? `-${formatNumber(summary.maxDrawdown, 0)}` : "0"}</b><small>从最高点回落</small></dd></div>
    <div><dt>100 次里赢</dt><dd><b>{wins == null ? "—" : `${wins} 次`}</b><small>共 {summary.trades} 笔</small></dd></div>
  </dl>;
}

/** AI 怎么看: the model's sentence (or the computed facts until / unless it comes), cautions in yellow. */
export function VerdictBlock({ result, asking }: { result: StrategyBacktestResult; asking: boolean }) {
  const verdict = result.verdict;
  if (!verdict) return null;
  const warnings = verdict.facts.filter(fact => fact.id === "tuned" || fact.id === "no_fees");
  const shown = verdict.text && verdict.source === "model"
    ? [{ text: verdict.text, warn: false }, ...warnings.filter(fact => !verdict.text!.includes(fact.text.slice(0, 6))).map(fact => ({ text: fact.text, warn: true }))]
    : verdict.facts.map(fact => ({ text: fact.text, warn: fact.tone === "warning" && (fact.id === "tuned" || fact.id === "no_fees") }));
  return <section className="sh-verdict" aria-label="AI 怎么看" data-agent-target="strategy-verdict">
    <b>AI 怎么看{asking ? <small>（正在请 AI 读这次结果…）</small> : null}</b>
    {shown.map((line, index) => <p key={index} className={line.warn ? "warn" : undefined}>{line.text}</p>)}
  </section>;
}

/** 试几组参数比一比: three groups of the key parameter, the current one marked, the held-out line below. */
export function CompareBlock({ card, disabled, onRun }: { card: CompareCard; disabled: boolean; onRun: () => void }) {
  const view = card.view;
  const done = view?.status === "done";
  return <section className="sh-tryit" aria-label="试几组参数比一比" data-agent-target="strategy-compare">
    <div className="sh-tryit-head"><b>试几组参数比一比</b>
      <HelpTip helpKey="strategy:compare" label="试几组参数比一比">对最关键的一个参数，跑当前值和上下各一组；前 7 成的行情挑，后 3 成单独检验。</HelpTip>
      {view?.paramHelp && view.paramLabel ? <ParamHelp paramKey="compare-param" label={view.paramLabel} help={view.paramHelp}/> : null}
      <button type="button" className="sh-btn" disabled={disabled || card.running} onClick={onRun}>
        {card.running ? "正在算…" : view ? "重新比" : "比一比"}</button></div>
    {view ? <table>
      <thead><tr><th scope="col">{view.paramLabel ?? "参数"}</th><th scope="col">赚</th><th scope="col">最多亏</th><th scope="col">胜率</th></tr></thead>
      <tbody>{view.rows.map(row => <tr key={row.variantId} className={row.current ? "best" : undefined}>
        <td>{row.label}</td>
        {row.summary ? <><td className={signClass(row.summary.netProfit)}>{cardMoney(row.summary.netProfit)}</td>
          <td className={row.summary.maxDrawdown ? "negative" : ""}>{row.summary.maxDrawdown ? `-${cardMoney(row.summary.maxDrawdown, false)}` : "0"}</td>
          <td>{percent(row.summary.winRate)}</td></>
          : <td colSpan={3}>{row.error ?? (card.running ? "算着…" : "—")}</td>}
      </tr>)}</tbody>
    </table> : null}
    {done && view?.line ? <span className="sh-hint" data-testid="strategy-compare-line">{view.line}</span> : null}
    {card.error ? <span className="sh-error" role="alert">{card.error}</span> : null}
  </section>;
}

/** 跟 AI 说怎么改: an input on the 结果页 (`disclosure: strategy-ask-ai`); sent as the user's turn with the strategy as focus. */
export function AskAiInput({ open, onOpenChange, disabled, onSend }: { open: boolean; onOpenChange: (open: boolean) => void; disabled: boolean; onSend: (text: string) => void }) {
  const [text, setText] = useState("");
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { if (open) input.current?.focus(); }, [open]);
  if (!open) return null;
  return <form className="sh-ask-ai" aria-label="跟 AI 说怎么改" onSubmit={event => { event.preventDefault(); const value = text.trim(); if (!value) return; onSend(value); setText(""); onOpenChange(false); }}>
    <input ref={input} value={text} maxLength={500} disabled={disabled} onChange={event => setText(event.target.value)}
      aria-label="跟 AI 说怎么改" placeholder="比如“把止损放宽一点再测一次”"/>
    <button type="submit" className="sh-btn primary" disabled={disabled || !text.trim()}><Send size={14}/>发给 AI</button>
  </form>;
}

/** The 结果页's own head: back to 我的策略, the strategy's name, its contract and period. */
export function ResultHead({ name, sub, state, onBack }: { name: string; sub: string; state: CardState; onBack: () => void }) {
  return <header className="sh-result-top">
    <button type="button" className="sh-back" aria-label="返回我的策略" onClick={onBack}><ArrowLeft size={14}/>我的策略</button>
    <span className="sh-result-name"><strong>{name}</strong><em className="sh-state" data-state={state.key}>{state.label}</em><small>{sub}</small></span>
  </header>;
}

/** 过去 6 个月按这个做 + the figures, the line and AI 怎么看 (or the 回测 entry before the first run). */
export function ResultSummary({ result, lots, running, error, asking, onRun, blocked }: {
  result?: StrategyBacktestResult; lots: number; running: boolean; error?: string; asking: boolean; onRun: () => void; blocked?: string | null;
}) {
  if (!result) return <section className="sh-result-empty" aria-label="回测">
    <h3>{running ? "正在回测最近 6 个月…" : "还没回测"}</h3>
    <span className="sh-hint">按规则回测最近 6 个月，每次 {lots} 手、扣手续费，看看按它做能不能赚钱。</span>
    {!running ? <button type="button" className="sh-btn primary" title={blocked ?? undefined} onClick={onRun}>回测</button> : null}
    {blocked ? <small role="status">{blocked}</small> : null}
    {error ? <p className="sh-error" role="alert">{error}</p> : null}
  </section>;
  const head = resultHead(result, lots);
  return <>
    <div className="sh-result-head"><h3>{head.title}</h3><span className="sh-hint">{head.hint}{running ? " · 正在重新回测…" : ""}</span></div>
    {error ? <p className="sh-error" role="alert">{error}</p> : null}
    <BigNumbers result={result}/>
    <div className="sh-curve" aria-label="资金曲线"><EquityLine points={result.equityCurve} tone={result.summary.netProfit >= 0 ? "up" : "down"}/></div>
    <VerdictBlock result={result} asking={asking}/>
  </>;
}

/** 正在运行 card body pieces: the latest signals as time · what happened. */
export function SignalRows({ rows }: { rows: { time: string; text: string }[] }) {
  if (!rows.length) return <p className="sh-sig-empty">今天还没有信号。</p>;
  return <div className="sh-sigs">{rows.map((row, index) => <div key={index} className="sh-sig"><time>{row.time}</time><span>{row.text}</span></div>)}</div>;
}


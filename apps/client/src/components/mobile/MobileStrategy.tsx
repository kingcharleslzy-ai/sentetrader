import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { FlaskConical, Plus, Send } from "lucide-react";
import { getStrategyMonitor, setStrategyMonitoring, stopStrategyMonitoring, type InstrumentCatalog, type StrategyDrawdown,
  type StrategyFunds, type StrategyMonitorSnapshot } from "../../api";
import { DrawdownStatus, FundsLines } from "../StrategyRiskBlocks";
import { launchBlockedReason, needsAnswersOf, periodLabel, resumeAfterDrawdown, strategiesChanged, strategyError, strategySymbol, strategyText, useStrategies, validateAndStart, type SavedStrategy } from "../../strategyApi";
import { runStrategyBacktest, useBacktestCard } from "../../strategyBacktests";
import { launchDefaults } from "../StrategySignalAction";
import { actionText, attemptStatusLabel, monitorTime } from "../../strategyMonitorDisplay";
import type { AiFocus } from "../AiMenu";
import { cardMoney } from "../StrategyHome";
import { contractLabel, formatSigned, moneyUnit, signClass } from "../futures/support";
import { PullIndicator, usePullToRefresh } from "./usePullToRefresh";

/** A running or paused strategy instance as the 策略 tab and AI 首页's 它正在盯着 show it. */
export interface MobileStrategyRun {
  strategyId: string;
  /** The 我的策略 entry it runs (the instance's own id for an older single-contract strategy). */
  libraryId: string;
  name: string;
  symbol: string;
  status: "monitoring" | "paused";
  /** The period it runs on (a follow-chart plan: the period it was bound to when it started). */
  timeframeSeconds: number | null;
  signalAction: "alert" | "confirm" | "auto";
  /** The instance's figure for the current trading day; null when the monitor gives none. */
  todayPnl: number | null;
  /** 按资金比例 / 回撤停机 as the Runtime reports them (null = not used). */
  funds: StrategyFunds | null;
  drawdown: StrategyDrawdown | null;
  /** The latest attempt: its time and what it did. */
  last: { time: string; text: string } | null;
  /** 策略页改版第八批 (2026-10-08): its own position, and the 今日判断 it goes against. */
  position: { direction: "long" | "short" | null; volume: number } | null;
  clash: string | null;
}

/** One 我的策略 entry on the phone: state, its sentence and the latest backtest. */
export interface MobileStrategyItem {
  strategyId: string; name: string; state: "运行中" | "已暂停" | "没上线"; plain: string | null;
  latest: SavedStrategy["latestBacktest"] | null;
  /** The saved strategy itself (编辑 / 回测 / 上线 read its plan and scope). */
  source: SavedStrategy;
}

const LIST_MS = 60_000;
const DETAIL_MS = 15_000;
const RUNNING = new Set(["monitoring", "paused"]);
const HOLDING = { long: "多", short: "空" } as const;

function runOf(item: SavedStrategy, library: SavedStrategy | undefined, monitor: StrategyMonitorSnapshot | undefined): MobileStrategyRun {
  const plan = item.definition.plan;
  const status = (monitor?.status ?? item.status) === "monitoring" ? "monitoring" : "paused";
  const pnl = Number(monitor?.today?.pnl);
  const attempt = monitor?.recentAttempts?.[0];
  const timeframe = plan?.timeframeSeconds || item.definition.chartTimeframeSeconds || null;
  return {
    strategyId: item.strategyId, libraryId: library?.strategyId ?? item.strategyId, name: strategyText(library?.name ?? item.name),
    symbol: item.symbol, status, timeframeSeconds: timeframe,
    signalAction: monitor?.effectiveSignalAction ?? monitor?.signalAction ?? item.signalAction ?? "auto",
    todayPnl: monitor?.today && monitor.today.pnl != null && Number.isFinite(pnl) ? pnl : null,
    last: attempt ? { time: monitorTime(attempt.updatedAt).slice(-8, -3),
      text: `${actionText(attempt.action, attempt.volume, Boolean(plan?.spread))} · ${attemptStatusLabel(attempt.status, attempt.cardProposalIds?.length, attempt.signalAction)}` } : null,
    position: monitor?.position ?? null, clash: monitor?.researchClash?.text ?? null,
    funds: monitor?.funds ?? null, drawdown: monitor?.drawdown ?? null,
  };
}

/**
 * 手机版 策略 (2026-10-06; 第八批 2026-10-08): the running and paused instances (`GET /api/strategy-plans`)
 * — each with its monitor (`GET /api/strategies/{id}/monitor`: 今日盈亏, position, the latest attempt, the
 * 今日判断 clash) while `detailed` — and 我的策略 (every library strategy with its latest backtest).
 * 暂停 / 继续运行 / 停止并平仓 are the running card's own calls (`POST …/monitor/pause|start|stop`; 继续
 * first checks AI 模拟执行 like the desktop's 继续; 停止并平仓 makes close cards to confirm, never an
 * order); a refusal stays on that card. The list is read again whenever a strategy changes in this window.
 * 手机编辑卡 (2026-10-08): 我的策略's 回测 is the desktop's 规则回测 (`runStrategyBacktest`, the chart's contract
 * when the scope allows it) and 上线… asks once, then runs the 回测结果页's launch — the same checks, 执行方式
 * fixed to 出卡确认.
 */
export function useMobileStrategies({ baseUrl, rootUrl, enabled, detailed, chartSymbol = "", chartTimeframe = 300, sessionId, backtestBlocked }: {
  baseUrl?: string; rootUrl?: string; enabled: boolean; detailed: boolean;
  /** The chart's contract and period: what 回测 and 上线 use when the strategy follows the chart. */
  chartSymbol?: string; chartTimeframe?: number;
  /** The conversation a launched instance sends its cards to. */
  sessionId?: string;
  /** The tier's reason line when 回测 is not part of it. */
  backtestBlocked?: string | null;
}) {
  const [runs, setRuns] = useState<MobileStrategyRun[]>([]);
  const [mine, setMine] = useState<MobileStrategyItem[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [notices, setNotices] = useState<Record<string, string>>({});
  const active = enabled && Boolean(baseUrl);
  const list = useStrategies(active ? baseUrl : undefined, detailed ? DETAIL_MS : LIST_MS);
  const failed = list.isError;
  useEffect(() => {
    if (!active || !baseUrl) { setRuns([]); setMine([]); setLoaded(false); return; }
    const result = list.data;
    // A failed read keeps what was shown.
    if (!result) { if (list.isError) setLoaded(true); return; }
    let alive = true;
    const library = result.items.filter(item => item.definition.plan);
    const executions = result.executions ?? [];
    const instances = [...executions, ...library.filter(item => !item.definition.instrumentScope)].filter(item => RUNNING.has(item.status));
    void (detailed ? Promise.all(instances.map(item => getStrategyMonitor(baseUrl, item.strategyId).catch(() => undefined))) : Promise.resolve([])).then(monitors => {
      if (!alive) return;
      const owner = (run: SavedStrategy) => library.find(item => item.strategyId === run.definition.libraryStrategyId);
      setRuns(instances.map((item, index) => runOf(item, owner(item), monitors[index])));
      setMine(library.map(item => {
        const own = instances.filter(run => (run.definition.libraryStrategyId ?? run.strategyId) === item.strategyId);
        return { strategyId: item.strategyId, name: strategyText(item.name), plain: item.planSummary ?? null, latest: item.latestBacktest ?? null, source: item,
          state: own.some(run => run.status === "monitoring") ? "运行中" : own.length ? "已暂停" : "没上线" };
      }));
      setLoaded(true);
    });
    return () => { alive = false; };
  }, [active, baseUrl, detailed, list.data, list.dataUpdatedAt, list.isError]);
  const { refetch } = list;
  const reload = useCallback(async () => { await refetch(); }, [refetch]);
  const act = useCallback(async (strategyId: string, action: "pause" | "start" | "resume-drawdown" | "stop" | "stop-close") => {
    if (!baseUrl || busy) return;
    setBusy(strategyId);
    setErrors(current => ({ ...current, [strategyId]: "" }));
    setNotices(current => ({ ...current, [strategyId]: "" }));
    try {
      if (action === "start" || action === "resume-drawdown") {
        const blocked = await launchBlockedReason(baseUrl);
        if (blocked) { setErrors(current => ({ ...current, [strategyId]: blocked })); return; }
      }
      if (action === "stop" || action === "stop-close") {
        const state = await stopStrategyMonitoring(baseUrl, strategyId, action === "stop-close");
        if (state.notice) setNotices(current => ({ ...current, [strategyId]: state.notice! }));
      } else if (action === "resume-drawdown") await resumeAfterDrawdown(baseUrl, strategyId);
      else await setStrategyMonitoring(baseUrl, strategyId, action);
      void strategiesChanged();
    } catch (error) {
      setErrors(current => ({ ...current, [strategyId]: strategyError(error, action === "pause" ? "暂停尚未确认，请刷新后重试"
        : action === "start" || action === "resume-drawdown" ? "没有继续运行，请刷新后重试" : "停止尚未确认，请刷新后重试") }));
    } finally { setBusy(null); }
  }, [baseUrl, busy]);
  const fail = (strategyId: string, text: string) => setErrors(current => ({ ...current, [strategyId]: text }));
  // 回测: the desktop card's 规则回测 (stored by the Runtime; the list's 最近回测 is read again after it).
  const backtest = useCallback((item: SavedStrategy) => {
    const plan = item.definition.plan;
    if (!rootUrl || !plan) return;
    if (backtestBlocked) { fail(item.strategyId, backtestBlocked); return; }
    if (needsAnswersOf(item)) { fail(item.strategyId, "策略还有待确认的条件，先点“编辑”完善再回测"); return; }
    fail(item.strategyId, "");
    void runStrategyBacktest(rootUrl, { strategyId: item.strategyId, symbol: strategySymbol(item, chartSymbol),
      ...(plan.timeframeSeconds ? {} : { timeframe: chartTimeframe }) })
      .then(result => { if (result) void strategiesChanged(); });
  }, [rootUrl, backtestBlocked, chartSymbol, chartTimeframe]);
  // 上线…: the card asks once (`launchFor`); 确认上线 = the 回测结果页's launch, 出卡确认 only.
  const [launchFor, setLaunchFor] = useState<string | null>(null);
  const launchSymbol = useCallback((item: SavedStrategy) => launchDefaults(item, chartSymbol).symbol || strategySymbol(item, chartSymbol), [chartSymbol]);
  const launch = useCallback(async (item: SavedStrategy) => {
    const id = item.strategyId;
    if (!baseUrl || busy) return;
    if (needsAnswersOf(item)) { fail(id, "策略还有待确认的条件，先点“编辑”完善再上线"); return; }
    setBusy(id); fail(id, "");
    setNotices(current => ({ ...current, [id]: "" }));
    try {
      const blocked = await launchBlockedReason(baseUrl);
      if (blocked) { fail(id, blocked); return; }
      await validateAndStart(baseUrl, id, launchSymbol(item), item.definition.plan?.timeframeSeconds || chartTimeframe, undefined,
        { signalAction: "confirm", ...(sessionId ? { sessionId } : {}) });
      setLaunchFor(null);
      setNotices(current => ({ ...current, [id]: "已上线：出卡确认，信号来了先出卡，你点确认才下单。" }));
      void strategiesChanged();
    } catch (error) { fail(id, strategyError(error, "验证未完成，请检查规则与行情连接后重试。")); }
    finally { setBusy(null); }
  }, [baseUrl, busy, chartTimeframe, sessionId, launchSymbol]);
  return useMemo(() => ({ runs, mine, loaded, failed, busy, errors, notices, reload, pause: (id: string) => act(id, "pause"), resume: (id: string) => act(id, "start"),
    resumeDrawdown: (id: string) => act(id, "resume-drawdown"),
    stop: (id: string, close: boolean) => act(id, close ? "stop-close" : "stop"), backtest, launchFor, askLaunch: setLaunchFor, launch, launchSymbol }),
  [runs, mine, loaded, failed, busy, errors, notices, reload, act, backtest, launchFor, launch, launchSymbol]);
}

export type MobileStrategies = ReturnType<typeof useMobileStrategies>;

function RunCard({ run, instruments, busy, error, notice, selected, onSelect, onPause, onResume, onResumeDrawdown, onStop, onChart }: {
  run: MobileStrategyRun; instruments?: InstrumentCatalog; busy: boolean; error?: string; notice?: string; selected: boolean;
  onSelect: () => void; onPause: () => void; onResume: () => void; onResumeDrawdown: () => void; onStop: (close: boolean) => void; onChart: () => void;
}) {
  const running = run.status === "monitoring";
  // 回撤停机 triggered: only 恢复运行 (a new 起始) brings it back, never the plain 继续运行.
  const halted = run.drawdown?.status === "liquidating" || run.drawdown?.status === "stopped";
  const [confirming, setConfirming] = useState(false);
  return <article className="fdm-strat" data-status={run.status} data-selected={selected || undefined} aria-label={run.name} data-testid="mobile-strategy-card">
    <button type="button" className="fdm-strat-head fdm-strat-pick" aria-pressed={selected} onClick={onSelect}>
      <strong>{run.name}</strong><em className="fdm-tag" data-tone={running ? "up" : "neutral"}>{running ? "运行中" : "已暂停"}</em></button>
    <div className="fdm-strat-meta">
      <span>{contractLabel(run.symbol, instruments)}{run.timeframeSeconds ? ` · ${periodLabel(run.timeframeSeconds)}` : ""}</span>
      {run.todayPnl !== null ? <span className={signClass(run.todayPnl)}>今天 {formatSigned(run.todayPnl, 0)}</span> : null}
      <span>{run.position?.direction ? `${HOLDING[run.position.direction]} ${run.position.volume} 手` : "没有持仓"}</span>
    </div>
    {run.clash ? <p className="fdm-strat-clash" role="note"><span aria-hidden="true">⚠</span>{run.clash}</p> : null}
    <FundsLines funds={run.funds} unit={moneyUnit(run.symbol)}/>
    <DrawdownStatus drawdown={run.drawdown} unit={moneyUnit(run.symbol)} busy={busy} columns={2} buttonClass="primary" onResume={onResumeDrawdown}/>
    {run.last ? <p className="fdm-strat-signal"><time>{run.last.time}</time> {run.last.text}</p> : null}
    {error ? <p className="fdm-strat-error" role="alert">{error}</p> : null}
    {notice ? <p className="fdm-strat-notice" role="status">{notice}</p> : null}
    {confirming ? <div className="fdm-strat-confirm" role="alertdialog" aria-label={`停止 ${run.name}`}>
      <p>停止后不再判断信号。{run.position?.direction ? `它还有 ${HOLDING[run.position.direction]} ${run.position.volume} 手，` : "它还有持仓的话，"}会在对话里出平仓卡，你确认了才平。</p>
      <div className="fdm-strat-actions">
        <button type="button" disabled={busy} onClick={() => setConfirming(false)}>取消</button>
        <button type="button" className="danger" disabled={busy} onClick={() => { setConfirming(false); onStop(true); }}>确认停止并平仓</button>
      </div>
    </div> : <div className="fdm-strat-actions">
      {halted ? null : running
        ? <button type="button" disabled={busy} onClick={onPause}>{busy ? "正在暂停…" : "暂停"}</button>
        : <button type="button" className="primary" disabled={busy} onClick={onResume}>{busy ? "正在继续…" : "继续运行"}</button>}
      <button type="button" className="danger" disabled={busy} onClick={() => setConfirming(true)}>停止并平仓</button>
      <button type="button" onClick={onChart}>看图表</button>
    </div>}
  </article>;
}

/** 我的策略 one card: state, its sentence, the latest backtest (a run just made here wins), 编辑 / 回测 / 上线…. */
function MineCard({ item, selected, onSelect, busy, error, notice, launching, launchText, onEdit, onBacktest, onLaunchAsk, onLaunch, onRunning }: {
  item: MobileStrategyItem; selected: boolean; onSelect: () => void; busy: boolean; error?: string; notice?: string;
  /** 上线… asked: the card shows what it will start and waits for 确认上线. */
  launching: boolean; launchText: string;
  onEdit: () => void; onBacktest: () => void; onLaunchAsk: (open: boolean) => void; onLaunch: () => void; onRunning: () => void;
}) {
  const card = useBacktestCard(item.strategyId);
  const run = card.result?.strategyId === item.strategyId ? card.result.summary : null;
  const latest = run ? { netProfit: run.netProfit, maxDrawdown: run.maxDrawdown, winRate: run.winRate } : item.latest;
  const error_ = error || card.error;
  return <article className="fdm-strat" aria-label={item.name} data-selected={selected || undefined} data-testid="mobile-strategy-mine">
    <button type="button" className="fdm-strat-head fdm-strat-pick" aria-pressed={selected} onClick={onSelect}>
      <strong>{item.name}</strong><em className="fdm-tag" data-tone={item.state === "运行中" ? "up" : "neutral"}>{item.state}</em></button>
    {item.plain ? <p>{item.plain}</p> : null}
    <div className="fdm-strat-meta">
      {latest ? <>
        <span>最近回测 <b className={signClass(latest.netProfit)}>{cardMoney(latest.netProfit)}</b></span>
        <span>最多亏 <b className={latest.maxDrawdown ? "negative" : ""}>{latest.maxDrawdown ? `-${cardMoney(latest.maxDrawdown, false)}` : "0"}</b></span>
        <span>胜率 <b>{latest.winRate == null ? "—" : `${Math.round(latest.winRate)}%`}</b></span>
      </> : <span>还没回测</span>}
    </div>
    {error_ ? <p className="fdm-strat-error" role="alert">{error_}</p> : null}
    {notice ? <p className="fdm-strat-notice" role="status">{notice}</p> : null}
    {launching ? <div className="fdm-strat-confirm fdm-strat-launch" role="alertdialog" aria-label={`上线 ${item.name}`}>
      <p>{launchText}</p>
      <div className="fdm-strat-actions">
        <button type="button" disabled={busy} onClick={() => onLaunchAsk(false)}>取消</button>
        <button type="button" className="primary" disabled={busy} onClick={onLaunch}>{busy ? "正在验证并上线…" : "确认上线"}</button>
      </div>
    </div> : <div className="fdm-strat-actions fdm-strat-actions3">
      <button type="button" disabled={busy} onClick={onEdit}>编辑</button>
      <button type="button" disabled={busy || card.running} onClick={onBacktest}>{card.running ? "回测中…" : "回测"}</button>
      {item.state === "没上线"
        ? <button type="button" disabled={busy} onClick={() => onLaunchAsk(true)}>上线…</button>
        : <button type="button" onClick={onRunning}>看运行中</button>}
    </div>}
  </article>;
}

/**
 * 策略 (手机版; 第八批 2026-10-08, the approved mockup): two tabs — 正在运行 (暂停 / 继续运行 / 停止并平仓 and
 * 看图表 on each card, the 今日判断 clash in yellow) and 我的策略 (state, the one sentence, the latest
 * backtest) — and at the bottom 跟 AI 说……: the text goes to the conversation with the selected
 * strategy as focus, and the AI changes it with the existing tools (a new version, then a backtest).
 * 手机编辑卡 (2026-10-08): 我的策略 starts with 新建策略 (继续编辑 while a changed draft is hidden) and each card
 * has 编辑 (the 策略编辑卡 full screen) / 回测 / 上线… (出卡确认 only, as on the 回测结果页).
 */
export function MobileStrategy({ strategies, instruments, blocked, tab, onTabChange, onChart, onAskAi, draftName, onNew, onResumeDraft, onEdit, starts }: {
  strategies: MobileStrategies;
  instruments?: InstrumentCatalog;
  /** The tier's reason line when strategies are not part of it. */
  blocked?: string | null;
  tab: "running" | "mine";
  onTabChange: (tab: "running" | "mine") => void;
  onChart: (run: MobileStrategyRun) => void;
  /** 跟 AI 说……: the text as the user's turn, about the selected strategy when there is one. */
  onAskAi: (text: string, focus?: AiFocus) => void;
  /** A hidden draft with changes (继续编辑：name); null when there is none. */
  draftName?: string | null;
  onNew?: () => void;
  onResumeDraft?: () => void;
  onEdit?: (item: SavedStrategy) => void;
  /** 我的策略 with no strategy: the ways to start (StrategyStarts, compact). */
  starts?: ReactNode;
}) {
  const pull = usePullToRefresh(strategies.reload, !blocked);
  const [picked, setPicked] = useState<string | null>(null);
  const [text, setText] = useState("");
  const fallback = strategies.runs[0]?.libraryId ?? strategies.mine[0]?.strategyId ?? null;
  const context = (picked && strategies.mine.some(item => item.strategyId === picked) ? picked : null) ?? fallback;
  const contextName = strategies.mine.find(item => item.strategyId === context)?.name
    ?? strategies.runs.find(run => run.libraryId === context)?.name;
  const send = () => {
    const value = text.trim();
    if (!value) return;
    onAskAi(value, context && contextName ? { kind: "strategy_edit", id: context, label: contextName } : undefined);
    setText("");
  };
  const running = strategies.runs;
  return (
    <section className="fdm-page fdm-strategy" aria-label="策略" data-testid="mobile-strategy" data-agent-target="mobile-strategy">
      <header className="fdm-head"><strong>策略</strong></header>
      <main className="fdm-content" ref={pull.ref}>
        <PullIndicator phase={pull.phase} />
        {blocked ? (
          // 当前档位不含策略 (10-06 实测：只剩一行原因、整页空白): what the page is for, why it is empty, where to look.
          <div className="fdm-empty" data-testid="mobile-strategy-blocked"><FlaskConical aria-hidden="true" />
            <strong>策略这一页看运行中的策略和我的策略</strong>
            <span>{blocked}</span>
            <span>档位与用量在电脑上的“设置 → 账户”里查看。</span>
          </div>
        ) : <>
          <div className="fdm-chips fdm-strat-tabs" role="group" aria-label="策略分页" data-agent-target="mobile-strategy-tabs">
            <button type="button" aria-pressed={tab === "running"} onClick={() => onTabChange("running")}>正在运行 {running.length || ""}</button>
            <button type="button" aria-pressed={tab === "mine"} onClick={() => onTabChange("mine")}>我的策略 {strategies.mine.length || ""}</button>
          </div>
          {strategies.failed && !strategies.runs.length && !strategies.mine.length ? <p className="fdm-note" role="alert">策略列表读取失败，稍后自动重试。</p> : null}
          {tab === "running" ? <>
            {running.map(run => <RunCard key={run.strategyId} run={run} instruments={instruments} busy={strategies.busy === run.strategyId}
              error={strategies.errors[run.strategyId]} notice={strategies.notices[run.strategyId]} selected={context === run.libraryId}
              onSelect={() => setPicked(run.libraryId)} onPause={() => void strategies.pause(run.strategyId)}
              onResume={() => void strategies.resume(run.strategyId)} onResumeDrawdown={() => void strategies.resumeDrawdown(run.strategyId)} onStop={close => void strategies.stop(run.strategyId, close)}
              onChart={() => onChart(run)} />)}
            {Object.entries(strategies.notices).filter(([id, notice]) => notice && !running.some(run => run.strategyId === id))
              .map(([id, notice]) => <p key={id} className="fdm-note" role="status">{notice}</p>)}
            {!running.length && strategies.loaded && !strategies.failed ? (
              <div className="fdm-empty"><FlaskConical aria-hidden="true" /><strong>没有正在运行的策略</strong><span>在电脑上点开一个策略，按“上线运行”。</span></div>
            ) : null}
          </> : <>
            {onNew ? <div className="fdm-strat-new-row">
              <button type="button" className="fdm-strat-new" onClick={() => onNew()}><Plus aria-hidden="true" />新建策略</button>
              {draftName ? <button type="button" className="fdm-strat-resume" onClick={onResumeDraft}>继续编辑：{draftName}</button> : null}
            </div> : null}
            {strategies.mine.map(item => <MineCard key={item.strategyId} item={item} selected={context === item.strategyId} onSelect={() => setPicked(item.strategyId)}
              busy={strategies.busy === item.strategyId} error={strategies.errors[item.strategyId]} notice={strategies.notices[item.strategyId]}
              launching={strategies.launchFor === item.strategyId}
              launchText={`在 ${contractLabel(strategies.launchSymbol(item.source), instruments)}${item.source.definition.plan?.timeframeSeconds ? ` · ${periodLabel(item.source.definition.plan.timeframeSeconds)}` : ""} 上线运行，执行方式固定“出卡确认”：信号来了先出卡，你点确认才下单。`}
              onEdit={() => onEdit?.(item.source)} onBacktest={() => strategies.backtest(item.source)}
              onLaunchAsk={open => strategies.askLaunch(open ? item.strategyId : null)} onLaunch={() => void strategies.launch(item.source)}
              onRunning={() => onTabChange("running")} />)}
            {!strategies.mine.length && strategies.loaded ? starts ?? <div className="fdm-empty"><FlaskConical aria-hidden="true" /><strong>还没有策略</strong><span>点“新建策略”，或在下面跟 AI 说一句你想怎么做。</span></div> : null}
          </>}
        </>}
      </main>
      {!blocked ? <form className="fdm-talk fdm-strat-talk" aria-label="跟 AI 说" onSubmit={event => { event.preventDefault(); send(); }}>
        {contextName ? <small className="fdm-strat-context">关于：{contextName}</small> : null}
        <input className="fdm-ask" value={text} maxLength={500} onChange={event => setText(event.target.value)}
          aria-label="跟 AI 说" placeholder={contextName ? "跟 AI 说：“把止损放宽一点再测一次”" : "跟 AI 说你想怎么做"} />
        <button type="submit" className="fdm-strat-send" aria-label="发送" disabled={!text.trim()}><Send aria-hidden="true" /></button>
      </form> : null}
    </section>
  );
}

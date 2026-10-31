import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { startupDuration } from "../userPreferences";
import { requestJson, type MarketWorkbenchSnapshot, type InstrumentCatalog } from "../api";
import { allowsSymbol, loadStrategies, strategyError, strategyScope, validateAndStart, type SavedStrategy } from "../strategyApi";
import { FeeEditor, StrategyDesigner } from "./StrategyWorkbench";
import "./HistoricalReplay.css";
import { HelpButton } from "./AppHelp";
import { useDialogSurface, useDisclosureSurface } from "../uiSurfaces";
import { StrategyItemActions } from "./StrategyItemActions";

interface ReplayState {
  replayId: string; status: "loading" | "ready" | "incomplete" | "unconfirmed" | "finished" | "error" | "closed";
  runtimePath: string; symbol: string; tradingDay: string; clock?: string;
  playing: boolean; speed: number; error?: string; notice?: string;
  entriesPaused?: boolean; ending?: boolean; ended?: boolean;
}
export interface ReplayWorkspaceProps {
  replayPath: string; replayControls?: ReactNode; onOpenReplay?: () => void;
  replayEnded?: boolean;
  chartTimeframe?: MarketWorkbenchSnapshot["durationSeconds"];
  onChartTimeframeChange?: (value: MarketWorkbenchSnapshot["durationSeconds"]) => void;
  onRuntimeReady: (url: string, scope?: string) => void; replaySymbol?: string;
}
function latestEndedDay() {
  // 昨天, and on a weekend the Friday before it (2026-10-03): the calendar read
  // replaces this guess when it answers; until then it is the honest default.
  // Counted on the Beijing calendar date: stepping in the machine's own time zone and only formatting in
  // Beijing gave a Saturday between Beijing 00:00 and the machine's midnight (a UTC machine late Sunday).
  const [year, month, date] = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai" }).format(new Date()).split("-").map(Number);
  const day = new Date(Date.UTC(year, month - 1, date - 1));
  while (day.getUTCDay() === 0 || day.getUTCDay() === 6) day.setUTCDate(day.getUTCDate() - 1);
  return day.toISOString().slice(0, 10);
}
export function HistoricalReplay({ enabled, environment = "local", children }: { enabled: boolean; environment?: string; children: (props: ReplayWorkspaceProps) => ReactNode }) {
  // The live workbench and a replay start on the account's default period (remembered on this machine).
  const [chartTimeframe, setChartTimeframe] = useState<MarketWorkbenchSnapshot["durationSeconds"]>(startupDuration);
  const [workspaceScope, setWorkspaceScope] = useState<string>();
  const [rootUrl, setRootUrl] = useState<string>();
  const [open, setOpen] = useState(false);
  const [replay, setReplay] = useState<ReplayState>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // The status poll's own trouble (#755 C): shown while it lasts, gone with the next good read or the replay.
  const [pollFailed, setPollFailed] = useState(false);
  const [symbol, setSymbol] = useState(() => localStorage.getItem("ai-trader:selected-instrument") || "SHFE.au2612");
  const [day, setDay] = useState(latestEndedDay);
  const [latestDay, setLatestDay] = useState(latestEndedDay);
  const dayTouched = useRef(false);
  const [balance, setBalance] = useState(1_000_000);
  const [mode, setMode] = useState("manual");
  const [plans, setPlans] = useState<SavedStrategy[]>([]);
  const [planId, setPlanId] = useState("");
  const [pendingPlan, setPendingPlan] = useState("");
  const [design, setDesign] = useState(false);
  const [catalog, setCatalog] = useState<InstrumentCatalog["items"]>([]);
  const [editingPlan, setEditingPlan] = useState<SavedStrategy>();
  const designArea = useRef<HTMLDivElement>(null);
  const inFlight = useRef(false);
  const dialog = useRef<HTMLDialogElement>(null);
  useDialogSurface("replay-setup", dialog, { open: () => setOpen(true), close: () => setOpen(false) });
  const [currentEnvironment, setCurrentEnvironment] = useState(environment);
  if (currentEnvironment !== environment) {
    // Retire replay data before rendering the new environment, without remounting
    // the workspace's login state and in-flight order guards.
    setCurrentEnvironment(environment);
    setRootUrl(undefined); setWorkspaceScope(undefined); setReplay(undefined);
    setOpen(false); setError(""); setPlans([]); setCatalog([]);
    setPlanId(""); setPendingPlan(""); setDesign(false); setEditingPlan(undefined);
  }
  const onRuntimeReady = useCallback((url: string, scope?: string) => {setRootUrl(url); setWorkspaceScope(scope);}, []);
  useEffect(() => { if (open) dialog.current?.showModal(); else dialog.current?.close(); }, [open]);
  useEffect(() => {
    if (!enabled || rootUrl === undefined) return;
    let cancelled = false;
    void requestJson<{ items: ReplayState[] }>(`${rootUrl}/api/replays`).then(({ items }) => { if (!cancelled && items[0]) setReplay(items[0]); }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [enabled, rootUrl]);
  useEffect(() => {
    if (!open || rootUrl === undefined) return;
    let cancelled = false;
    void Promise.all([loadStrategies(rootUrl), requestJson<InstrumentCatalog>(`${rootUrl}/api/instruments`)]).then(([saved, instruments]) => {
      if (!cancelled) { setPlans(saved.items.filter(p => p.definition.plan)); setCatalog(instruments.items); }
    }).catch(() => !cancelled && setError("配置读取不完整，请检查连接"));
    // 默认日期 = 最近一个已结束交易日 (2026-10-03): the sidecar's calendar knows the
    // holidays and, as latestClosedTradingDay, whether today's day session has closed
    // (its 15:30 rule, the one the replay itself applies — #754; an older sidecar sends
    // only previousTradingDay); an unreadable calendar keeps the weekend fallback above,
    // and a day the user already picked is never overwritten.
    void requestJson<{ previousTradingDay?: string; latestClosedTradingDay?: string }>(`${rootUrl}/api/market/calendar`)
      .then(({ previousTradingDay, latestClosedTradingDay }) => {
        const latest = latestClosedTradingDay ?? previousTradingDay;
        if (cancelled || typeof latest !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(latest)) return;
        setLatestDay(latest);
        if (!dayTouched.current) setDay(latest);
      }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [open, rootUrl]);
  useEffect(() => {
    if (!replay || rootUrl === undefined) return;
    let cancelled = false; let timer: ReturnType<typeof setTimeout>;
    const poll = async () => { try {
      const next = await requestJson<ReplayState>(`${rootUrl}/api/replays/${replay.replayId}`);
      if (!cancelled) { setReplay(next); setPollFailed(false); }
    } catch { if (!cancelled) setPollFailed(true); }
    finally { if (!cancelled) timer = setTimeout(poll, 750); } };
    void poll(); return () => { cancelled = true; clearTimeout(timer); setPollFailed(false); };
  }, [rootUrl, replay?.replayId]);
  useEffect(() => {
    if (!pendingPlan || replay?.status !== "ready" || rootUrl === undefined || inFlight.current) return;
    const id = pendingPlan; setPendingPlan(""); inFlight.current = true; setBusy(true);
    void validateAndStart(`${rootUrl}${replay.runtimePath}`, id, replay.symbol, chartTimeframe).then(() => requestJson<ReplayState>(`${rootUrl}/api/replays/${replay.replayId}/control`, { method: "POST", body: JSON.stringify({ action: "play" }) })).then(setReplay).catch(() => setError("策略验证或启动未完成，回放已保留并暂停；请在策略卡检查条件后启动。")).finally(() => { inFlight.current = false; setBusy(false); });
  }, [pendingPlan, replay?.status, rootUrl]);
  const create = async () => {
    if (inFlight.current || rootUrl === undefined || replay) return;
    inFlight.current = true; setBusy(true); setError("");
    try {
      // The displayed period opens first; the remaining periods keep loading in
      // the background once the sidecar reaches ready on this one. An unusable
      // period value falls back to 60s instead of blocking the replay.
      const defaultDurationSeconds = chartTimeframe || 60;
      const next = await requestJson<ReplayState>(`${rootUrl}/api/replays`, { method: "POST", body: JSON.stringify({ symbol: symbol.trim(), tradingDay: day, initialBalance: balance, defaultDurationSeconds }) });
      setReplay(next); setOpen(false); setPendingPlan(mode === "ai" ? planId : "");
    } catch (failure) {
      // A 422 is the Runtime refusing the day itself (holiday, not closed yet) with a Chinese reason that
      // names the days that work: nothing was created, show the reason as it is.
      const refusal = (failure as { status?: number; detail?: { detail?: unknown } }).status === 422
        ? (failure as { detail?: { detail?: unknown } }).detail?.detail : undefined;
      if (typeof refusal === "string" && /[一-龥]/.test(refusal)) { setError(refusal); return; }
      try { const { items } = await requestJson<{ items: ReplayState[] }>(`${rootUrl}/api/replays`); if (items[0]) { setReplay(items[0]); setOpen(false); } } catch { /* Preserve uncertainty; never retry creation. */ }
      setError("回放没有创建成功：历史行情服务这次没有确认。可以稍后再点一次，或换一个更早的交易日；不会用示例数据代替。");
    } finally { inFlight.current = false; setBusy(false); }
  };
  const control = async (action: string, speed?: number) => {
    if (!replay || inFlight.current || rootUrl === undefined) return;
    inFlight.current = true; setBusy(true); setError("");
    try { const next = await requestJson<ReplayState>(`${rootUrl}/api/replays/${replay.replayId}/control`, { method: "POST", body: JSON.stringify({ action, ...(speed !== undefined ? { speed } : {}) }) });
      if (action === "close") { setReplay(undefined); setOpen(false); } else setReplay(next);
    } catch { setError("操作结果未确认，已保留回放；请查看状态，不要重复下单。"); }
    finally { inFlight.current = false; setBusy(false); }
  };
  const matchingPlans = plans.filter(p => allowsSymbol(p, symbol));
  const chosen = matchingPlans.find(p => p.strategyId === planId);
  const needsAnswers = Boolean(chosen?.definition.plan?.questions.length || chosen?.definition.plan?.clarifications?.length);
  const blockedReason = rootUrl === undefined ? "正在连接回放服务…" : !symbol.trim() ? "请填写期货合约。" : !day ? "请选择交易日。" : day > latestDay ? "请选择已收盘的交易日。" : !Number.isFinite(balance) || balance < 100000 || balance > 100000000 ? "初始资金须为 10 万至 1 亿元。" : mode === "ai" && !chosen ? "请选择一条策略，或先设计新策略。" : "";
  const beginDesign = (item?: SavedStrategy) => { setEditingPlan(item); setDesign(true); setError(""); };
  // #520-D.5: the replay's designer is an edit session like the workbench's — closing it, opening it
  // again or switching the edited plan ends the session, so a late save only adds the plan to the list
  // instead of closing or selecting in whatever is open now.
  const editSession = useRef(0);
  useEffect(() => { editSession.current++; }, [design, editingPlan?.strategyId]);
  useEffect(() => () => { editSession.current++; }, []);
  // Round twelve: the designer inside the replay dialog and its fee section answer `disclosure` commands.
  const designAvailable = open && rootUrl !== undefined && !replay;
  useDisclosureSurface("replay-design", { available: designAvailable, state: design, apply: wanted => { if (wanted) beginDesign(); else { setDesign(false); setEditingPlan(undefined); } } });
  const [feesOpen, setFeesOpen] = useDisclosureSurface("replay-fees", { available: designAvailable && !design });
  useEffect(() => { if (design) designArea.current?.scrollIntoView?.({block: "nearest"}); }, [design, editingPlan?.strategyId]);
  const removeChosen = async () => {
    if (!chosen || rootUrl === undefined || inFlight.current) return;
    const id = chosen.strategyId; inFlight.current = true; setBusy(true); setError("");
    try {
      await requestJson(`${rootUrl}/api/strategy-plans/${id}`, {method: "DELETE"});
      setPlans(old => old.filter(item => item.strategyId !== id)); setPlanId("");
      if (editingPlan?.strategyId === id) { setDesign(false); setEditingPlan(undefined); }
      window.dispatchEvent(new Event("ai-trader-strategies-changed"));
    } catch (error) { setError(strategyError(error, "策略删除失败，请刷新后重试")); }
    finally { inFlight.current = false; setBusy(false); }
  };
  // incomplete (#932 B3): the last step stopped part-way; play/step finish that minute first.
  const steppable = replay?.status === "ready" || replay?.status === "incomplete";
  const banner = error || (pollFailed && replay ? "回放连接暂时不可用，正在重试；回放里的委托只进回放模拟盘。" : "");
  const controls = <div className="replay-host">
    {replay && <section className="historical-replay" data-help="replay" aria-label="历史回放控制"><div className="historical-replay-controls"><strong>历史回放 · 独立模拟盘</strong><span>{replay.symbol} · {replay.tradingDay}</span><output>{replay.clock ? new Date(replay.clock).toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "载入历史行情…"}</output>
      <button disabled={busy || !steppable || replay.ended} onClick={() => void control(replay.playing ? "pause" : "play")}>{replay.playing ? "暂停" : "播放"}</button><button disabled={busy || !steppable || replay.playing || replay.ended} onClick={() => void control("step")}>单步</button>
      <select aria-label="回放速度" disabled={busy} value={replay.speed} onChange={e => void control("speed", Number(e.target.value))}>{[1, 2, 5, 10].map(n => <option key={n} value={n}>{n} 步/秒</option>)}</select>
      <button disabled={busy || replay.ended || replay.ending} onClick={() => void control(replay.entriesPaused ? "resume_entries" : "pause_entries")}>{replay.entriesPaused ? "恢复新开仓" : "暂停新开仓"}</button>
      <button className="app-button danger" disabled={busy || replay.ended || replay.ending || replay.status === "loading" || replay.status === "incomplete" || replay.status === "unconfirmed"} onClick={() => void control("finish")}>{replay.ended ? "已停盘 · 可复盘" : replay.ending ? "正在平仓停盘…" : "平仓并停盘"}</button><button disabled={busy} onClick={() => void control("close")}>退出回放</button>
    <HelpButton topic="replay" /></div>{replay.notice || replay.error ? <p role="alert">{replay.notice || replay.error}</p> : null}</section>}
    {banner && !open && <p className="replay-error" role="alert">{banner}</p>}
    <dialog ref={dialog} className={`replay-setup ${design ? "strategy-design-dialog" : ""}`} onCancel={() => setOpen(false)}><header><div><h2>{design ? "设计策略" : "历史回放设置"}</h2>{!design&&<HelpButton topic="replay"/>}</div><button aria-label="关闭回放设置" onClick={() => setOpen(false)}>×</button></header>
      {design ? <div className="replay-setup-body">          {rootUrl !== undefined && <div ref={designArea} className="replay-strategy-designer"><button type="button" className="replay-design-close" onClick={() => setDesign(false)}>返回回放设置</button><StrategyDesigner key={editingPlan?.strategyId || "new"} draftKey={editingPlan?.strategyId} initialPlan={editingPlan?.definition.plan} initialScope={editingPlan ? strategyScope(editingPlan) : undefined} workspaceScope={workspaceScope} rootUrl={rootUrl} symbol={symbol} editSession={() => editSession.current} onSaved={(item, session) => { setPlans(old => [...old.filter(p => p.strategyId !== item.strategyId), item]); window.dispatchEvent(new Event("ai-trader-strategies-changed")); if (session !== editSession.current) return; setPlanId(item.strategyId); setDesign(false); setEditingPlan(undefined); }}/></div>}</div> : replay ? <div className="replay-setup-body"><p>当前回放仍在运行。请先停盘复盘，再退出并新建。</p><button onClick={() => setOpen(false)}>返回回放</button></div> : <><div className="replay-setup-body">
        <div className="replay-mode-options"><button aria-pressed={mode === "manual"} className={mode === "manual" ? "selected" : ""} onClick={() => setMode("manual")}>自己交易</button><button aria-pressed={mode === "ai"} className={mode === "ai" ? "selected" : ""} onClick={() => setMode("ai")}>AI 自动策略</button></div>
        <div className="replay-fields"><label>期货合约<input list="replay-contracts" value={symbol} onChange={e => { setSymbol(e.target.value); setPlanId(""); }} placeholder="搜索或输入具体合约"/><datalist id="replay-contracts">{catalog.map(item => <option key={item.symbol} value={item.symbol}>{item.instrumentName}</option>)}</datalist></label><label>交易日<input type="date" value={day} max={latestDay} onChange={e => { dayTouched.current = true; setDay(e.target.value); }}/></label><label>初始资金 · 元<input type="number" min="100000" max="100000000" step="10000" value={balance} onChange={e => setBalance(Number(e.target.value))}/></label></div>
        {mode === "ai" && <section className="replay-strategy-picker">
          <StrategyItemActions id={`replay:${planId}`} name={chosen?.name || "所选策略"} hideActions={!chosen} disabled={busy || !chosen} onDelete={removeChosen}><label>选择已保存策略<select value={planId} disabled={busy} onChange={e => { setPlanId(e.target.value); setDesign(false); setEditingPlan(undefined); setError(""); }}><option value="">请选择策略</option>{matchingPlans.map(item => <option key={item.strategyId} value={item.strategyId}>{item.name} · v{item.version}{item.definition.plan?.questions.length || item.definition.plan?.clarifications?.length ? " · 待完善" : ""}</option>)}</select></label></StrategyItemActions>
          <div className="replay-strategy-actions">{chosen && <><button type="button" disabled={busy} onClick={() => beginDesign(chosen)}>{needsAnswers ? "完善所选策略" : "修改所选策略"}</button></>}<button type="button" disabled={busy} onClick={() => beginDesign()}>设计新策略</button></div>
          {needsAnswers && <p className="replay-strategy-notice" role="status">这条策略还有待确认的买卖条件，完善后才能开始回放。</p>}

        </section>}
        {rootUrl !== undefined && <details className="replay-fee-details" open={feesOpen} onToggle={e => setFeesOpen(e.currentTarget.open)}><summary>手续费</summary><FeeEditor rootUrl={rootUrl} symbol={symbol}/></details>}
        {error && <p role="alert">{error}</p>}
      </div><div className="replay-start-actions">
          {blockedReason && <p className="replay-blocked-reason" role="status">{blockedReason}</p>}
          <button className="strategy-primary" disabled={busy || Boolean(blockedReason)} onClick={() => mode === "ai" && needsAnswers ? beginDesign(chosen) : void create()}>{busy ? "正在处理…" : mode === "ai" ? needsAnswers ? "完善策略后回放" : "验证并开始策略回放" : "开始手动回放"}</button>
        </div></>}
    </dialog>
  </div>;
  return children({ chartTimeframe, onChartTimeframeChange: setChartTimeframe, replayPath: replay?.runtimePath || "", replaySymbol: replay?.symbol, replayEnded: replay?.ended || replay?.status === "finished",
    replayControls: enabled && (open || replay || banner) ? controls : undefined, onOpenReplay: enabled ? () => setOpen(true) : undefined, onRuntimeReady });
}

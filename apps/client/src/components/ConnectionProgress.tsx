import { useEffect, useRef, useState } from "react";
import { CircleCheck, CircleAlert, LoaderCircle } from "lucide-react";
import type { RuntimeStatus } from "@ai-trader/ui-events";
import type { AgentAccountStatus } from "../api";
import "./ConnectionProgress.css";
import { tqkqRecovery } from "../dataConnectionStatus";

/** TqSdk cold start in the user's words: the sidecar's fine-grained stages fold into three phases. */
const STARTUP_PHASES: { name: string; stages: string[] }[] = [
  { name: "登录快期", stages: ["starting", "loading_sdk", "authenticating"] },
  { name: "合约目录", stages: ["subscribing", "discovering_instruments"] },
  { name: "首批行情", stages: ["waiting_snapshot", "waiting_account", "waiting_market"] },
];
const STARTUP_STAGES = STARTUP_PHASES.flatMap(phase => phase.stages);
const COLD_START_KEY = "ai-trader:cold-start-seconds";

/** Seconds the previous cold start of this account mode took; undefined until one has completed. */
function readLastColdStart(mode: string | undefined): number | undefined {
  try {
    const value = Number(window.localStorage.getItem(`${COLD_START_KEY}:${mode ?? "unknown"}`));
    return Number.isFinite(value) && value > 0 ? value : undefined;
  } catch { return undefined; }
}
function rememberColdStart(mode: string | undefined, seconds: number) {
  try { window.localStorage.setItem(`${COLD_START_KEY}:${mode ?? "unknown"}`, String(Math.round(seconds))); } catch { /* optional */ }
}

/** The three phases with their state and, once done, how long each took (from the sidecar's timings). */
export function startupPhases(runtime: RuntimeStatus | undefined) {
  const stage = runtime?.dataConnectionStage;
  const active = stage ? STARTUP_STAGES.indexOf(stage) : -1;
  if (!stage || active < 0) return undefined;
  const timings = runtime?.dataStartupTimings ?? {};
  return STARTUP_PHASES.map(phase => {
    const first = STARTUP_STAGES.indexOf(phase.stages[0]);
    const last = STARTUP_STAGES.indexOf(phase.stages[phase.stages.length - 1]);
    const state: "done" | "active" | "pending" = active > last ? "done" : active >= first ? "active" : "pending";
    const seconds = phase.stages.reduce((sum, name) => sum + (timings[name] ?? 0), 0);
    return { name: phase.name, state, seconds: state === "done" ? Math.round(seconds) : undefined };
  });
}

export function ConnectionProgress({ connection, runtime, agent, hasMarket, onSettings, runtimeUsable }: {
  connection: "connecting" | "connected" | "disconnected";
  runtime?: RuntimeStatus;
  agent?: AgentAccountStatus;
  hasMarket: boolean;
  onSettings: () => void;
  runtimeUsable?: boolean;
}) {
  const serviceReady = connection === "connected" && runtimeUsable !== false;
  // A closed session counts as connected whether the sidecar reached "ready" with cached quotes or
  // logged in after the day close and is waiting for the first quote (TqKq clears quotes at
  // settlement; a fresh session between 15:15 and the night open has none until the bell).
  const closed = runtime?.marketSessionStatus === "closed" && (runtime.dataConnectionStage === "ready" || runtime.dataConnectionStage === "waiting_market");
  const failed = runtime?.dataConnectionStage === "failed";
  const reconnecting = runtime?.dataConnectionStage === "reconnecting";
  const accountReady = serviceReady && !failed && !reconnecting && (runtime?.accountStatus === "normal" || (closed && runtime.accountStatus === "stale"));
  // The feed's own status decides; a single contract without a snapshot is reported by the chart area, not here.
  const dataReady = serviceReady && !failed && !reconnecting && (runtime?.marketDataStatus === "normal" || (closed && runtime.marketDataStatus === "stale"));
  // 新浪备用 (2026-10-07): TqKq is down and the market shows Sina's quotes — a warning, not a spinner.
  const dataBackup = serviceReady && runtime?.marketDataStatus === "backup";
  // Open market, TqKq data on hand but not updating (e.g. a source just back but full or warming up
  // still answers its old packet): say so as a warning, not "连接中".
  const dataStale = serviceReady && !failed && !closed && runtime?.marketDataStatus === "stale";
  const waiting = runtime?.dataConnectionStage && !["failed", "ready"].includes(runtime.dataConnectionStage) && !closed;
  const agentReady = serviceReady && agent?.authenticated === true;
  const agentFailed = Boolean(agent && !agent.authenticated && (agent.error || agent.configurationRequired || agent.loginRequired));
  const stages = [
    { name: "应用服务", ready: serviceReady, failed: connection === "disconnected" },
    { name: runtime?.accountMode === "fake" ? "演示账户" : runtime?.accountMode === "paper" ? "本站账户" : "快期账户", ready: accountReady, failed: failed || (!waiting && runtime?.accountStatus === "unavailable") },
    { name: "行情", ready: dataReady, warning: dataBackup ? "备用（新浪，可能延迟几秒）" : dataStale ? "快期数据暂时没更新" : undefined,
      failed: !dataBackup && (failed || (!waiting && runtime?.marketDataStatus === "unavailable")) },
    { name: "AI 模型", ready: agentReady, failed: agentFailed || connection === "disconnected" },
  ];
  const pending = stages.some(stage => !stage.ready && !stage.failed && !("warning" in stage && stage.warning));
  const allReady = stages.every(stage => stage.ready);
  // 已等待 counts from when the connection problem began (#712 B7): retries flip stages between
  // 连接中 and 需要处理, which must not restart it; a fully ready connection clears it, and switching to
  // another trading account starts a new count (a gap with no account id is not a switch).
  const accountId = runtime?.account?.id;
  const sinceAccount = useRef(accountId);
  const [since, setSince] = useState<number | undefined>(() => allReady ? undefined : Date.now());
  useEffect(() => {
    const switched = accountId !== undefined && sinceAccount.current !== undefined && accountId !== sinceAccount.current;
    if (accountId !== undefined) sinceAccount.current = accountId;
    setSince(previous => allReady ? undefined : switched ? Date.now() : previous ?? Date.now());
  }, [allReady, accountId]);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!pending) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [pending]);
  const seconds = since === undefined ? 0 : Math.max(0, Math.floor((now - since) / 1000));
  // Cold-start phases and the estimate from the previous start of this account mode.
  const phases = closed ? undefined : startupPhases(runtime);
  const coldStarting = Boolean(phases) && runtime?.accountMode !== "fake";
  const elapsed = runtime?.dataInitializationSeconds != null ? Math.max(runtime.dataInitializationSeconds, seconds) : seconds;
  const lastColdStart = coldStarting ? readLastColdStart(runtime?.accountMode) : undefined;
  const remaining = lastColdStart !== undefined ? Math.max(0, Math.round(lastColdStart - elapsed)) : undefined;
  const overdue = lastColdStart !== undefined && elapsed > lastColdStart * 2;
  useEffect(() => {
    if (runtime?.dataConnectionStage === "ready" && runtime.dataStartupTotalSeconds) rememberColdStart(runtime.accountMode, runtime.dataStartupTotalSeconds);
  }, [runtime?.dataConnectionStage, runtime?.dataStartupTotalSeconds, runtime?.accountMode]);
  if (allReady) return null;
  const recovery = tqkqRecovery(runtime, serviceReady);
  return <section className={`fdw-connection-progress ${hasMarket ? "" : "is-initial"}`} aria-label="连接进度">
    <div className="fdw-connection-stages" role="status">
      {stages.map(stage => {
        const warning = "warning" in stage ? stage.warning : undefined;
        return <span key={stage.name} data-state={stage.ready ? "ready" : warning ? dataBackup ? "backup" : "stale" : stage.failed ? "failed" : "pending"}>
          {stage.ready ? <CircleCheck /> : stage.failed || warning ? <CircleAlert /> : <LoaderCircle className="connection-spinner" />}
          {stage.name} · {stage.ready ? (closed && stage.name === "行情" ? "休市" : "已就绪") : warning ?? (stage.failed ? "需要处理" : "连接中")}
        </span>;
      })}
    </div>
    {coldStarting && phases ? <ol className="fdw-startup-phases" aria-label="行情连接阶段">
      {phases.map(phase => <li key={phase.name} data-state={phase.state}>
        {phase.state === "done" ? <CircleCheck /> : phase.state === "active" ? <LoaderCircle className="connection-spinner" /> : <span className="dot" aria-hidden="true" />}
        {phase.name}{phase.state === "done" && phase.seconds !== undefined ? ` · ${phase.seconds} 秒` : phase.state === "active" ? " · 进行中" : ""}
      </li>)}
    </ol> : null}
    {(!accountReady || !dataReady) && <p>{connection === "connecting" ? "正在连接应用服务" : runtime?.accountMode === "fake" && serviceReady ? "正在读取演示账户与行情" : recovery.detail}</p>}
    {pending && <small aria-hidden="true">已等待 {seconds} 秒{coldStarting ? remaining !== undefined ? overdue ? "，已超过上次启动用时的两倍" : `，预计还需约 ${remaining} 秒` : "，首次启动需要 1–2 分钟" : ""}</small>}
    {connection === "disconnected" || overdue || stages.some(stage => stage.failed) ? <button type="button" onClick={onSettings}>查看连接设置</button> : null}
  </section>;
}

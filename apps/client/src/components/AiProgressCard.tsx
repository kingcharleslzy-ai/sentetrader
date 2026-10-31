import { useEffect, useState } from "react";
import { useDisclosureSurface } from "../uiSurfaces";
import "./AiProgressCard.css";

export interface AiProgress {
  status: "running" | "completed" | "failed" | "cancelled";
  stage?: string;
  summary?: string;
  startedAt?: string;
  finishedAt?: string;
}
export function AiProgressCard({ task, title = "AI 正在处理", onCancel, steps = [], id }: {
  task: AiProgress; title?: string; onCancel?: () => void | Promise<unknown>;
  steps?: { id: string; label: string; status: string; detail: string }[];
  /** Card id for the `disclosure: progress-steps` command. */
  id?: string;
}) {
  const [now, setNow] = useState(Date.now());
  const [expanded, setExpanded] = useDisclosureSurface("progress-steps", { id, available: Boolean(id && (task.summary || steps.length > 0)) });
  const [cancelling, setCancelling] = useState(false);
  const active = task.status === "running";
  useEffect(() => { if (!active) { setExpanded(false); setCancelling(false); return; } const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, [active, setExpanded]);
  const elapsed = task.startedAt ? Math.max(0, Math.floor(((task.finishedAt ? Date.parse(task.finishedAt) : now) - Date.parse(task.startedAt)) / 1000)) : undefined;
  const status = { running: "处理中", completed: "已完成", failed: "失败", cancelled: "已停止" }[task.status];
  const stage = task.stage || [...steps].reverse().find(s => s.status === "active")?.label || (active ? "等待模型响应" : status);
  return <section className={`ai-progress-card ${task.status}`} aria-label="AI 处理进度" data-testid="ai-progress-card">
    <header><strong>{active && <i/>}{active ? title : status}</strong><span>{elapsed !== undefined && Number.isFinite(elapsed) ? `${elapsed} 秒` : status}</span></header>
    <p role="status">{stage}</p>
    {(task.summary || steps.length > 0) && <details open={expanded} onToggle={e => setExpanded(e.currentTarget.open)}><summary>{task.summary ? "查看思考摘要与过程" : "查看处理过程"}</summary>
      {task.summary && <p className="ai-progress-summary">{task.summary}</p>}
      {steps.length > 0 && <ul>{steps.map(s => <li key={s.id}><span>{s.status === "done" ? "✓" : s.status === "failed" ? "!" : "·"}</span>{s.label}<small>{s.detail}</small></li>)}</ul>}
    </details>}
    {active && onCancel && <button type="button" disabled={cancelling} onClick={async () => { setCancelling(true); try { await onCancel(); } finally { setCancelling(false); } }}>{cancelling ? "正在停止…" : "停止生成"}</button>}
  </section>;
}

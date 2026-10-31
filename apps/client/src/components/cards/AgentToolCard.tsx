import { TerminalSquare } from "lucide-react";
import type { AgentToolActivity } from "@ai-trader/ui-events";
import { CardShell } from "../CardShell";
import { useDisclosureSurface } from "../../uiSurfaces";

export function AgentToolCard({ activity, id }: { activity: AgentToolActivity; id?: string }) {
  const completed = activity.status === "completed";
  const [open, setOpen] = useDisclosureSurface("tool-card", { id, available: Boolean(id && activity.outputSummary) });
  return (
    <CardShell
      icon={<TerminalSquare />}
      title="命令"
      badge={completed ? "已完成" : activity.status === "failed" ? "失败" : "运行中"}
      badgeTone={completed ? "positive" : activity.status === "failed" ? "danger" : "neutral"}
      testId="agent-tool-card"
    >
      <div className="agent-summary">
        <code>{activity.command || "等待命令信息…"}</code>
        {activity.exitCode !== undefined && activity.exitCode !== null ? <span>退出码 {activity.exitCode}</span> : null}
      </div>
      {activity.outputSummary ? (
        <details className="detail-disclosure" open={open} onToggle={e => setOpen(e.currentTarget.open)}>
          <summary>查看命令输出</summary>
          <pre>{activity.outputSummary}</pre>
        </details>
      ) : null}
    </CardShell>
  );
}

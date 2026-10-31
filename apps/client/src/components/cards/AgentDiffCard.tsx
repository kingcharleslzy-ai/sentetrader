import { FileDiff } from "lucide-react";
import type { AgentDiffArtifact } from "@ai-trader/ui-events";
import { CardShell } from "../CardShell";
import { useDisclosureSurface } from "../../uiSurfaces";

export function AgentDiffCard({ artifact, id }: { artifact: AgentDiffArtifact; id?: string }) {
  // One state for every file of the card: the command opens or folds them all.
  const [open, setOpen] = useDisclosureSurface("diff-card", { id, available: Boolean(id && artifact.changes.length) });
  return (
    <CardShell
      icon={<FileDiff />}
      title={`文件修改 · ${artifact.changes.length} 个文件`}
      badge={artifact.status === "completed" ? "已应用" : artifact.status === "failed" ? "失败" : "处理中"}
      badgeTone={artifact.status === "completed" ? "positive" : artifact.status === "failed" ? "danger" : "neutral"}
      testId="agent-diff-card"
    >
      <div className="diff-file-list">
        {artifact.changes.map((change) => (
          <details className="detail-disclosure" key={`${change.kind}:${change.path}`} open={open} onToggle={e => setOpen(e.currentTarget.open)}>
            <summary><span className="diff-kind">{change.kind}</span>{change.path}</summary>
            <pre>{change.diff || "没有可展示的 Diff。"}</pre>
          </details>
        ))}
      </div>
    </CardShell>
  );
}

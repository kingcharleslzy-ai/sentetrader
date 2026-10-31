import { CircleAlert } from "lucide-react";
import type { RuntimeError } from "@ai-trader/ui-events";
import { CardShell } from "../CardShell";

export function TaskErrorCard({ error, id }: { error: RuntimeError; id?: string }) {
  const title = error.scope === "agent" ? "模型状态异常" : "工程任务未完成";
  return (
    <CardShell
      icon={<CircleAlert />}
      title={title}
      badge="应用服务在线"
      badgeTone="warning"
      id={id}
      testId="task-error-card"
      details={<dl className="card-facts"><div><dt>错误代码</dt><dd>{error.code}</dd></div></dl>}
    >
      <p className="warning-copy">{error.message}</p>
    </CardShell>
  );
}

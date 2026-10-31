import { useEffect, useState } from "react";
import { getAgentAccount, type AgentAccountStatus } from "../api";
import type { WorkspaceHandle } from "./workspaceHandle";

/**
 * The model account behind the connected Runtime, polled every 2 minutes (it changes only when the
 * owner reconfigures the model pool). Model keys live with the
 * Runtime (cloud model pool); when the model is not ready the client can only say so.
 */
export function useAgentAccount({ handle }: { handle: WorkspaceHandle }) {
  const { baseUrl, connection, setToast } = handle;
  const [agentAccount, setAgentAccount] = useState<AgentAccountStatus>();

  useEffect(() => {
    if (connection !== "connected") {
      setAgentAccount(undefined);
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    const load = async () => {
      try {
        const status = await getAgentAccount(baseUrl);
        if (cancelled) return;
        setAgentAccount(status);
      } catch {
        if (!cancelled) setAgentAccount({ available: false, authenticated: false, loginRequired: false, error: "agent_status_unavailable" });
      } finally {
        if (!cancelled) timer = window.setTimeout(() => void load(), 120_000);
      }
    };
    void load();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [baseUrl, connection]);

  const requireModel = (hint: string) => setToast(hint, "warning");
  return { agentAccount, requireModel };
}

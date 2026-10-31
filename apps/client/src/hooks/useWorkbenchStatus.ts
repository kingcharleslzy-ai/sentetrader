import { useEffect, useMemo, useState } from "react";
import type { RuntimeStatus } from "@ai-trader/ui-events";
import type { ConnectionState } from "./useRuntimeSession";

export type WorkbenchDataStatus = RuntimeStatus["accountStatus"] | RuntimeStatus["marketDataStatus"] | "unknown";

export interface WorkbenchStatus {
  now: number;
  runtimeLive: boolean;
  runtimeFresh: boolean;
  runtimeUsable: boolean;
  agentStatus: "connected" | "offline" | "unknown";
  accountStatus: WorkbenchDataStatus;
  marketStatus: WorkbenchDataStatus;
  riskStatus: RuntimeStatus["riskStatus"];
}

interface WorkbenchStatusInput {
  connection: ConnectionState;
  runtime?: RuntimeStatus;
  runtimeUpdatedAt?: string;
  runtimeActivityAt?: number;
  now?: number;
}

function isFresh(timestamp: number | undefined, now: number): boolean {
  return typeof timestamp === "number"
    && Number.isFinite(timestamp)
    && now - timestamp < 30_000;
}

export function deriveWorkbenchStatus({
  connection,
  runtime,
  runtimeUpdatedAt,
  runtimeActivityAt,
  now = Date.now()
}: WorkbenchStatusInput): WorkbenchStatus {
  const eventTimestamp = runtimeUpdatedAt ? new Date(runtimeUpdatedAt).valueOf() : undefined;
  const runtimeFresh = isFresh(runtimeActivityAt, now) || isFresh(eventTimestamp, now);
  const runtimeLive = connection === "connected" && runtime?.runtimeConnected === true;
  const runtimeUsable = runtimeLive && runtimeFresh;

  return {
    now,
    runtimeLive,
    runtimeFresh,
    runtimeUsable,
    agentStatus: !runtime
      ? "unknown"
      : runtimeUsable && runtime.agentConnected
        ? "connected"
        : "offline",
    accountStatus: runtimeUsable ? runtime?.accountStatus ?? "unknown" : "unknown",
    marketStatus: runtimeUsable ? runtime?.marketDataStatus ?? "unknown" : "unknown",
    riskStatus: runtimeUsable ? runtime?.riskStatus ?? "unknown" : "unknown"
  };
}

export function useWorkbenchStatus(input: Omit<WorkbenchStatusInput, "now">): WorkbenchStatus {
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 10_000);
    return () => window.clearInterval(timer);
  }, []);

  return useMemo(
    () => deriveWorkbenchStatus({ ...input, now }),
    [input.connection, input.runtime, input.runtimeActivityAt, input.runtimeUpdatedAt, now]
  );
}

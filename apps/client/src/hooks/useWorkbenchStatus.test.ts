// @vitest-environment node
import type { RuntimeStatus } from "@ai-trader/ui-events";
import { deriveWorkbenchStatus } from "./useWorkbenchStatus";

const runtime: RuntimeStatus = {
  mode: "local",
  adapter: "native-agent",
  accountMode: "tqkq-readonly",
  runtimeConnected: true,
  agentConnected: true,
  marketDataStatus: "normal",
  accountStatus: "normal",
  tradingGatewayStatus: "read_only",
  riskStatus: "normal",
  connected: true,
  version: "0.8.0"
};

test("derives all workbench states from the same freshness boundary", () => {
  const now = Date.now();
  const fresh = deriveWorkbenchStatus({
    connection: "connected",
    runtime,
    runtimeUpdatedAt: new Date(now - 2_000).toISOString(),
    now
  });
  expect(fresh).toMatchObject({
    runtimeLive: true,
    runtimeFresh: true,
    runtimeUsable: true,
    agentStatus: "connected",
    accountStatus: "normal",
    marketStatus: "normal",
    riskStatus: "normal"
  });

  const stale = deriveWorkbenchStatus({
    connection: "connected",
    runtime,
    runtimeUpdatedAt: new Date(now - 31_000).toISOString(),
    now
  });
  expect(stale).toMatchObject({
    runtimeLive: true,
    runtimeFresh: false,
    runtimeUsable: false,
    agentStatus: "offline",
    accountStatus: "unknown",
    marketStatus: "unknown",
    riskStatus: "unknown"
  });
});

import { defineConfig, devices } from "@playwright/test";

// Same local fake-data runtime as playwright.config.ts, plus the in-process
// simulation gateway so the execution chain (activate / stop / order links)
// runs for real. Kept on its own ports and state root: with a gateway present
// the runtime refuses fixture quotes for order preparation, which the plain
// suite relies on.
const nodeProcess = (globalThis as {
  process?: { env?: Record<string, string | undefined> }
}).process;
const environment = nodeProcess?.env || {};
const reuseExistingServer = environment.CI !== "true";
const runtimePort = environment.AI_TRADER_EXECUTION_E2E_RUNTIME_PORT || "8003";
const clientPort = environment.AI_TRADER_EXECUTION_E2E_CLIENT_PORT || "4176";
const stateParent = (environment.RUNNER_TEMP || environment.TMPDIR || "/tmp").replace(/\/$/, "");
const stateRoot = `${stateParent}/ai-trader-playwright-execution-${environment.GITHUB_RUN_ID || "local"}-${environment.GITHUB_RUN_ATTEMPT || "1"}`;
if (nodeProcess?.env) nodeProcess.env.AI_TRADER_E2E_STATE_ROOT = stateRoot;

export default defineConfig({
  testDir: "./tests",
  // The voice orb and AI annotation specs live here too: they need a Runtime nobody else's beforeEach
  // deletes sessions from (the main suite runs files in parallel workers and its preamble deletes the latest session).
  testMatch: ["execution-ai-trader.spec.ts", "ai-orb.spec.ts", "ai-annotations.spec.ts", "settings-preferences.spec.ts", "ui-coverage.spec.ts"],
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 8_000 },
  fullyParallel: false,
  globalTeardown: "./playwright.global-teardown.ts",
  reporter: "line",
  use: {
    baseURL: `http://127.0.0.1:${clientPort}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure"
  },
  projects: [
    { name: "chromium-execution", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }
  ],
  webServer: [
    {
      command: `AI_TRADER_LOCAL_TOKEN=ai-trader-execution-e2e-token AI_TRADER_AGENT_MODE=fake AI_TRADER_FAKE_SIMULATION_GATEWAY=1 AI_TRADER_FAKE_RESEARCH_LIVE=1 AI_TRADER_ALLOWED_ORIGINS=http://127.0.0.1:${clientPort} AI_TRADER_STATE_ROOT='${stateRoot}' AI_TRADER_DB_PATH='${stateRoot}/ai-trader.sqlite3' uv run --project ../../services/trader-runtime uvicorn trader_runtime.main:app --host 127.0.0.1 --port ${runtimePort}`,
      url: `http://127.0.0.1:${runtimePort}/healthz`,
      reuseExistingServer,
      timeout: 120_000
    },
    {
      command: `VITE_LOCAL_RUNTIME_URL=http://127.0.0.1:${runtimePort} VITE_LOCAL_RUNTIME_TOKEN=ai-trader-execution-e2e-token VITE_CLOUD_RUNTIME_URL=http://127.0.0.1:${runtimePort} VITE_ALLOW_INSECURE_LOCAL_CLOUD=1 VITE_CLOUD_RUNTIME_TOKEN=ai-trader-execution-e2e-token VITE_CLOUD_AUTH_MODE=local-token npm run dev -- --host 127.0.0.1 --port ${clientPort}`,
      url: `http://127.0.0.1:${clientPort}`,
      reuseExistingServer,
      timeout: 120_000
    }
  ]
});

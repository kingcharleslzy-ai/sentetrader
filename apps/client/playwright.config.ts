import { defineConfig, devices } from "@playwright/test";

const nodeProcess = (globalThis as {
  process?: { env?: Record<string, string | undefined> }
}).process;
const environment = nodeProcess?.env || {};
const reuseExistingServer = environment.CI !== "true";
const runtimePort = environment.AI_TRADER_E2E_RUNTIME_PORT || "8000";
const clientPort = environment.AI_TRADER_E2E_CLIENT_PORT || "4173";
const stateParent = (environment.RUNNER_TEMP || environment.TMPDIR || "/tmp").replace(/\/$/, "");
const stateRoot = `${stateParent}/ai-trader-playwright-${environment.GITHUB_RUN_ID || "local"}-${environment.GITHUB_RUN_ATTEMPT || "1"}`;
if (nodeProcess?.env) nodeProcess.env.AI_TRADER_E2E_STATE_ROOT = stateRoot;

export default defineConfig({
  testDir: "./tests",
  testIgnore: [
    "cloud-tauri-ai-trader.spec.ts",
    "cloud-compose.spec.ts",
    "execution-ai-trader.spec.ts",
    "ai-orb.spec.ts",
    "ai-annotations.spec.ts",
    "ui-coverage.spec.ts",
    "settings-preferences.spec.ts",
  ],
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
    { name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }
  ],
  webServer: [
    {
      command: `AI_TRADER_LOCAL_TOKEN=ai-trader-e2e-local-token AI_TRADER_AGENT_MODE=fake AI_TRADER_ALLOWED_ORIGINS=http://127.0.0.1:${clientPort} AI_TRADER_STATE_ROOT='${stateRoot}' AI_TRADER_DB_PATH='${stateRoot}/ai-trader.sqlite3' uv run --project ../../services/trader-runtime uvicorn trader_runtime.main:app --host 127.0.0.1 --port ${runtimePort}`,
      url: `http://127.0.0.1:${runtimePort}/healthz`,
      reuseExistingServer,
      timeout: 120_000
    },
    {
      command: `VITE_LOCAL_RUNTIME_URL=http://127.0.0.1:${runtimePort} VITE_LOCAL_RUNTIME_TOKEN=ai-trader-e2e-local-token VITE_CLOUD_RUNTIME_URL=http://127.0.0.1:${runtimePort} VITE_ALLOW_INSECURE_LOCAL_CLOUD=1 VITE_CLOUD_RUNTIME_TOKEN=ai-trader-e2e-local-token VITE_CLOUD_AUTH_MODE=local-token npm run dev -- --host 127.0.0.1 --port ${clientPort}`,
      url: `http://127.0.0.1:${clientPort}`,
      reuseExistingServer,
      timeout: 120_000
    }
  ]
});

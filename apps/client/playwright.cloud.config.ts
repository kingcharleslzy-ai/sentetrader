import { createHmac } from "node:crypto";
import { defineConfig, devices } from "@playwright/test";

const secret = "phase-16-e2e-cloud-secret-with-more-than-32-bytes";
const encode = (value: object) => Buffer.from(JSON.stringify(value)).toString("base64url");
const header = encode({ alg: "HS256", typ: "JWT" });
const payload = encode({
  iss: "ai-trader-cloud",
  aud: "ai-trader",
  tid: "tauri-e2e-tenant",
  sub: "desktop-user",
  iat: Math.floor(Date.now() / 1000),
  exp: Math.floor(Date.now() / 1000) + 3600
});
const signature = createHmac("sha256", secret)
  .update(`${header}.${payload}`)
  .digest("base64url");
const token = `${header}.${payload}.${signature}`;
const cloudPort = process.env.AI_TRADER_CLOUD_E2E_PORT || "8443";
const cloudRuntimeUrl = `https://127.0.0.1:${cloudPort}`;
const localRuntimePort = process.env.AI_TRADER_E2E_RUNTIME_PORT || "8000";
const reuseExistingServer =
  (globalThis as { process?: { env?: { CI?: string } } }).process?.env?.CI !== "true";

export default defineConfig({
  testDir: "./tests",
  testMatch: "cloud-tauri-ai-trader.spec.ts",
  timeout: 45_000,
  expect: { timeout: 12_000 },
  reporter: "line",
  use: {
    baseURL: "http://tauri.localhost:4174",
    ignoreHTTPSErrors: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure"
  },
  projects: [
    { name: "chromium-cloud-tauri", use: { ...devices["Desktop Chrome"] } }
  ],
  webServer: [
    {
      command: `AI_TRADER_LOCAL_TOKEN=ai-trader-cloud-e2e-local-token AI_TRADER_AGENT_MODE=fake AI_TRADER_ALLOWED_ORIGINS=http://tauri.localhost:4174 uv run --project ../../services/trader-runtime uvicorn trader_runtime.main:app --host 127.0.0.1 --port ${localRuntimePort}`,
      url: `http://127.0.0.1:${localRuntimePort}/healthz`,
      reuseExistingServer,
      timeout: 120_000
    },
    {
      command: "node tests/support/cloud-runtime.mjs",
      url: `${cloudRuntimeUrl}/healthz`,
      ignoreHTTPSErrors: true,
      reuseExistingServer: false,
      timeout: 120_000
    },
    {
      command: `VITE_LOCAL_RUNTIME_URL=http://127.0.0.1:${localRuntimePort} VITE_LOCAL_RUNTIME_TOKEN=ai-trader-cloud-e2e-local-token VITE_CLOUD_RUNTIME_URL=${cloudRuntimeUrl} VITE_CLOUD_RUNTIME_TOKEN=${token} VITE_CLOUD_AUTH_MODE=cloud-jwt VITE_DEFAULT_RUNTIME_MODE=cloud npm run dev -- --host 0.0.0.0 --port 4174`,
      url: "http://127.0.0.1:4174",
      reuseExistingServer: false,
      timeout: 120_000
    }
  ]
});

import { defineConfig } from "@playwright/test";

const viewports = [
  [1920, 1080],
  [1366, 768],
  [960, 720],
  [768, 900],
  [414, 896],
  [375, 812],
  [320, 720],
] as const;

export default defineConfig({
  testDir: "./tests",
  testMatch: "cloud-compose.spec.ts",
  timeout: 45_000,
  expect: { timeout: 15_000 },
  reporter: "line",
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: process.env.AI_TRADER_CLOUD_SMOKE_URL || "http://127.0.0.1:8080",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: viewports.map(([width, height]) => ({
    name: `compose-${width}x${height}`,
    use: { viewport: { width, height } },
  })),
});

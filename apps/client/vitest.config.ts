import { availableParallelism } from "node:os";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import clientPackage from "./package.json";

const repositoryRoot = decodeURIComponent(new URL("../..", import.meta.url).pathname).replace(/^\/([A-Za-z]:\/)/, "$1").replace(/\/$/, "");

export default defineConfig({
  resolve: { alias: { "@ai-trader/ui-events": decodeURIComponent(new URL("../../packages/ui-events/src/index.ts", import.meta.url).pathname).replace(/^\/([A-Za-z]:\/)/, "$1") } },
  plugins: [react()],
  // Same build identity as vite.config.ts; tests build without AI_TRADER_BUILD_SHA → "dev".
  define: {
    __AI_TRADER_CLIENT_VERSION__: JSON.stringify(clientPackage.version),
    __AI_TRADER_BUILD_SHA__: JSON.stringify(
      (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env?.AI_TRADER_BUILD_SHA || "dev")
  },
  test: {
    // happy-dom by default (about 40% less environment/test time than jsdom here). A file that
    // needs no DOM opts into `// @vitest-environment node`; a file whose assertions depend on
    // jsdom keeps `// @vitest-environment jsdom`.
    environment: "happy-dom",
    setupFiles: "./src/test/setup.ts",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
    // At least two workers: vitest's default (cores - 1) runs a single worker on the 2-vCPU CI
    // runner. Files stay isolated; isolate:false made ~570 tests leak mocks into each other.
    pool: "threads",
    maxWorkers: Math.max(2, availableParallelism() - 1),
    minWorkers: 1,
    // Catches hangs only; a test over 2 s is listed by the reporter (slowTestThreshold) as a soft rule.
    testTimeout: 10_000,
    slowTestThreshold: 2_000,
    // `vitest --changed <ref>` (scripts/test_changed.mjs, PR CI) runs every test when a changed
    // file matches one of these, not only the tests importing it. vitest's defaults end in "/**"
    // and so never match a changed file; the patterns are anchored at the repository root because
    // a bare "**" does not cross dot directories (agent worktrees live under .claude/ and .codex/).
    // The setup file is added by vitest itself.
    forceRerunTriggers: [
      "**/package.json",
      "**/package-lock.json",
      "**/{vitest,vite}.config.*",
      "**/tsconfig*.json",
      // Read by tests without a graph edge: GuestEntry loads styles.css with readFileSync (CSS
      // imports are blanked), and two tests import Runtime sources with `?raw`.
      "**/*.css",
      "**/trader_runtime/strategy_rules.py",
      "**/trader_runtime/simulation_orders.py"
    ].map((pattern) => `${repositoryRoot}/${pattern}`)
  }
});

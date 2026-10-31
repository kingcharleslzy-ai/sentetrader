import { spawnSync } from "node:child_process";
import { rmSync } from "node:fs";
import { join } from "node:path";
import { ensureDesktopBuildLock } from "./desktop_build_lock.mjs";

await ensureDesktopBuildLock({ snapshot: true });
if (process.env.CARGO_TARGET_DIR) rmSync(join(process.env.CARGO_TARGET_DIR, "release/bundle"), { recursive: true, force: true });
const prepared = spawnSync("npm", ["--workspace", "@ai-trader/desktop", "run", "prepare:sidecars"], { stdio: "inherit" });
if (prepared.error) throw prepared.error;
if (prepared.status !== 0) process.exit(prepared.status ?? 1);

// CI builds the updater payload too (AI Trader.app.tar.gz) with a disposable key, so the release
// script can reuse the exact bundle and only sign it with the real key; local builds stay plain.
const script = process.env.AI_TRADER_DESKTOP_BUILD_UPDATER === "1" ? "build:update" : "build:app";
const result = spawnSync(
  "npm",
  ["--workspace", "@ai-trader/desktop", "run", script],
  {
    stdio: "inherit",
    env: { ...process.env, CI: process.env.CI || "true" },
  },
);

if (result.error) throw result.error;
process.exit(result.status ?? 1);

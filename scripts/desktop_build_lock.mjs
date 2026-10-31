import { spawn } from "node:child_process";
import { fstatSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";

export async function ensureDesktopBuildLock({ snapshot = false } = {}) {
  // Windows CI uses its own target and does not share the Mac build directory.
  if (process.platform === "win32") return;
  const target = process.env.CARGO_TARGET_DIR || join(homedir(), ".cache/ai-trader/cargo-target");
  const descriptor = Number(process.env.AI_TRADER_BUILD_LOCK_FD);
  if (Number.isInteger(descriptor) && descriptor > 2) {
    const inherited = fstatSync(descriptor);
    const expected = statSync(join(target, ".ai-trader-build.lock"));
    if (inherited.dev !== expected.dev || inherited.ino !== expected.ino) throw new Error("desktop_build_lock_mismatch");
    return;
  }
  const args = [fileURLToPath(new URL("./desktop_build_lock.py", import.meta.url))];
  if (snapshot) args.push("--snapshot", process.env.AI_TRADER_DESKTOP_ARTIFACT_DIR
    || join(homedir(), ".cache/ai-trader/desktop-candidates", randomUUID()));
  args.push("--", process.execPath, ...process.argv.slice(1));
  const child = spawn(process.env.AI_TRADER_BUILD_PYTHON || "python3", args, { stdio: "inherit" });
  const terminate = () => child.kill("SIGTERM");
  const interrupt = () => child.kill("SIGINT");
  process.on("SIGTERM", terminate);
  process.on("SIGINT", interrupt);
  const status = await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", code => resolve(code ?? 1));
  });
  process.off("SIGTERM", terminate);
  process.off("SIGINT", interrupt);
  process.exit(status);
}

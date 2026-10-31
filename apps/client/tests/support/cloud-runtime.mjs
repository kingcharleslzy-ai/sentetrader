import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";

const temporary = mkdtempSync(join(tmpdir(), "ai-trader-cloud-e2e-"));
const key = join(temporary, "runtime.key");
const certificate = join(temporary, "runtime.crt");
const repoRoot = resolve(process.cwd(), "../..");
const secret = "phase-16-e2e-cloud-secret-with-more-than-32-bytes";
const port = process.env.AI_TRADER_CLOUD_E2E_PORT || "8443";

const generated = spawnSync("openssl", [
  "req", "-x509", "-newkey", "rsa:2048", "-nodes",
  "-keyout", key, "-out", certificate, "-days", "1",
  "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"
], { stdio: "ignore" });
if (generated.status !== 0) {
  rmSync(temporary, { recursive: true, force: true });
  throw new Error("cloud_e2e_certificate_generation_failed");
}

const child = spawn("uv", [
  "run", "--project", "services/trader-runtime", "uvicorn", "trader_runtime.main:app",
  "--host", "127.0.0.1", "--port", port,
  "--ssl-keyfile", key, "--ssl-certfile", certificate
], {
  cwd: repoRoot,
  stdio: "inherit",
  env: {
    ...process.env,
    AI_TRADER_AUTH_MODE: "cloud-jwt",
    AI_TRADER_AGENT_MODE: "fake",
    AI_TRADER_CLOUD_JWT_SECRET: secret,
    AI_TRADER_ALLOWED_ORIGINS: "http://tauri.localhost:4174",
    AI_TRADER_RUNTIME_STATE_ROOT: join(temporary, "state")
  }
});

let stopping = false;
function stop(signal = "SIGTERM") {
  if (stopping) return;
  stopping = true;
  if (!child.killed) child.kill(signal);
  setTimeout(() => {
    rmSync(temporary, { recursive: true, force: true });
    process.exit(0);
  }, 200).unref();
}

process.on("SIGINT", () => stop("SIGINT"));
process.on("SIGTERM", () => stop("SIGTERM"));
child.on("exit", (code) => {
  rmSync(temporary, { recursive: true, force: true });
  process.exit(code ?? 1);
});

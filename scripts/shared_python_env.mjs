// Shared exact-lock Python environments (AGENTS.md "Dependencies and builds"; #932 B7/B11, 2026-10-19).
//
// One environment per project lock: ~/.cache/ai-trader/python-deps/<sha256 of the project's uv.lock>,
// marked complete by a copy of that lock (ai-trader-uv.lock) — the directory
// scripts/build_desktop_sidecars.py reads for the packaged Runtime. It is synced with
// --no-install-project, so no editable install binds it to one checkout's sources; callers put
// their own checkout's src/ on PYTHONPATH. test:changed runs the Python tests in it (a fresh
// per-checkout .venv paid ~30–50 s on its first `import tqsdk`, macOS scanning every newly written
// native library once), and release:all's preflight builds the Runtime's before the desktop segment
// needs it.
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const PYTHON_DEPS = join(homedir(), ".cache", "ai-trader", "python-deps");
export const LOCK_MARKER = "ai-trader-uv.lock";
/** The extras each project's shared environment carries (one environment per lock, so one set). */
export const PROJECT_EXTRAS = { "services/trader-runtime": [], "services/tqkq-sidecar": ["--extra", "tqkq"] };
const PROJECT_DIST = { "services/trader-runtime": "ai_trader_runtime", "services/tqkq-sidecar": "ai_trader_tqkq_sidecar" };

export function sharedEnvironmentPath(root, project, depsRoot = PYTHON_DEPS) {
  return join(depsRoot, createHash("sha256").update(readFileSync(join(root, project, "uv.lock"))).digest("hex"));
}

/** Complete for this lock: the marker equals the lock and the venv exists (the same test as
 *  build_desktop_sidecars.shared_dependency_environment). */
export function sharedEnvironmentReady(root, project, depsRoot = PYTHON_DEPS) {
  const environment = sharedEnvironmentPath(root, project, depsRoot);
  const marker = join(environment, LOCK_MARKER);
  return existsSync(marker) && existsSync(join(environment, "pyvenv.cfg"))
    && readFileSync(marker).equals(readFileSync(join(root, project, "uv.lock")));
}

/** The project itself installed (editable .pth or dist-info) ties an environment to one checkout. */
export function projectInstalled(environment, dist) {
  const lib = join(environment, process.platform === "win32" ? "Lib" : "lib");
  if (!existsSync(lib)) return false;
  const sites = process.platform === "win32" ? [join(lib, "site-packages")]
    : readdirSync(lib).filter((name) => name.startsWith("python")).map((name) => join(lib, name, "site-packages"));
  return sites.filter(existsSync).some((site) => readdirSync(site).some((name) => {
    const lower = name.toLowerCase();
    return lower.startsWith(`${dist}-`) || lower.startsWith(`_${dist}`) || lower.startsWith(`__editable__.${dist}`);
  }));
}

/** Syncs the project's shared environment from its lock (a no-op well under a second when complete;
 *  it also finishes one an interrupted run left half-built) and marks it complete. Returns
 *  `{ environment, created, seconds }`, or throws `shared_python_env_unusable:<why>`. */
export function ensureSharedEnvironment(root, project, { depsRoot = PYTHON_DEPS, sync = spawnSync, log = console.log } = {}) {
  const lock = readFileSync(join(root, project, "uv.lock"));
  const environment = sharedEnvironmentPath(root, project, depsRoot);
  const created = !sharedEnvironmentReady(root, project, depsRoot);
  mkdirSync(depsRoot, { recursive: true });
  const started = Date.now();
  const result = sync("uv", ["sync", "--frozen", "--no-install-project", "--quiet", "--project", join(root, project),
    ...(PROJECT_EXTRAS[project] ?? [])], { cwd: root, stdio: "inherit", env: { ...process.env, UV_PROJECT_ENVIRONMENT: environment } });
  if (result.status !== 0) throw new Error(`shared_python_env_unusable:uv sync failed for ${project} (${environment})`);
  if (PROJECT_DIST[project] && projectInstalled(environment, PROJECT_DIST[project])) {
    throw new Error(`shared_python_env_unusable:${environment} has ${project} itself installed (bound to one checkout)`);
  }
  const seconds = Math.round((Date.now() - started) / 1000);
  if (created) {
    const marker = join(environment, LOCK_MARKER);
    writeFileSync(`${marker}.${process.pid}`, lock);
    renameSync(`${marker}.${process.pid}`, marker);
    log(`${project}: 按锁建好共享 Python 环境 ${environment}（${seconds}s）`);
  }
  return { environment, created, seconds };
}

/** Environment variables for `uv run --no-sync --project <project>` in the shared environment. */
export function sharedRunEnv(root, project, environment) {
  const source = join(root, project, "src");
  return { UV_PROJECT_ENVIRONMENT: environment,
    PYTHONPATH: process.env.PYTHONPATH ? `${source}${delimiter}${process.env.PYTHONPATH}` : source };
}

// `node scripts/shared_python_env.mjs services/trader-runtime`: build or finish one project's shared
// environment by hand (prepare:sidecars outside release:all).
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const project = process.argv[2];
  if (!(project in PROJECT_EXTRAS)) {
    console.error(`usage: node scripts/shared_python_env.mjs ${Object.keys(PROJECT_EXTRAS).join("|")}`);
    process.exit(2);
  }
  const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
  console.log(JSON.stringify(ensureSharedEnvironment(root, project)));
}

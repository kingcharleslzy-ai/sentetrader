// One local test entry (2026-10-11): `npm run test:changed [-- --base <ref>] [--all]`.
// Runs only the tests a change can reach, stops at the first failure, and always ends with
// one summary line (exit code included) — never rerun a suite just to find its result. Tests over
// 2 s (soft rule) are listed: vitest marks them inline (slowTestThreshold), pytest prints --durations.
//
// Changed files = `<base>...HEAD` plus staged, unstaged and untracked files, renames listed as
// deletion plus addition (`--no-renames`, as CI does). Areas and Python test
// files come from the same plan CI uses (scripts/ci_select_checks.mjs planChecks), so a
// CI/build/root-script change still selects every area:
//   client    vitest --changed <base> (import graph; vitest.config.ts forceRerunTriggers run all)
//   runtime   pytest on the affected test files: changed tests, tests whose imports reach a changed
//   sidecar   module, tests naming a changed text file by path; whole area where the plan cannot
//             place a change (conftest, __init__, pyproject/lock, package data no test names by
//             full path, workspace/)
//   workspace the workspace suite (two tests)
// --all runs the selected areas in full; --dry-run prints the commands without running them.
//
// Python environments (2026-10-19, #932 B11): runtime and sidecar tests run in the shared exact-lock
// environment ~/.cache/ai-trader/python-deps/<sha256 of the project's uv.lock> (scripts/shared_python_env.mjs;
// the directory prepare:sidecars reads) with this checkout's src/ on PYTHONPATH, not a fresh
// per-checkout .venv, whose first `import tqsdk` took ~50 s. Missing → built once from the lock. A
// checkout that already has its own .venv keeps using it; an unusable shared one falls back to it.

import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, rmSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { planChecks, PYTHON } from "./ci_select_checks.mjs";
import { ensureSharedEnvironment, PROJECT_EXTRAS, sharedEnvironmentPath, sharedRunEnv } from "./shared_python_env.mjs";

const root = fileURLToPath(new URL("..", import.meta.url));
const args = process.argv.slice(2);
const all = args.includes("--all");
const dryRun = args.includes("--dry-run"); // print the plan, run nothing
const baseIndex = args.indexOf("--base");

function git(...gitArgs) {
  return execFileSync("git", gitArgs, { cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] })
    .split("\n").filter(Boolean);
}

function refExists(ref) {
  try {
    git("rev-parse", "--verify", "--quiet", `${ref}^{commit}`);
    return true;
  } catch {
    return false;
  }
}

// Default base: origin/main, else a local main (a checkout without the remote ref).
const base = baseIndex >= 0 ? args[baseIndex + 1] : ["origin/main", "main"].find(refExists);
if (!base || !refExists(base)) {
  console.log(`test:changed 汇总：找不到比较基准 ${base ?? "origin/main / main"}，未运行测试；用 -- --base <ref> 指定 → exit 2`);
  process.exit(2);
}

const changed = [...new Set([
  ...git("diff", "--name-only", "--no-renames", `${base}...HEAD`),
  ...git("diff", "--name-only", "--no-renames", "--cached"),
  ...git("diff", "--name-only", "--no-renames"),
  ...git("ls-files", "--others", "--exclude-standard"),
])];

// Local pytest workers: the development Macs have 10+ cores (CI sets its own count for the VM).
const WORKERS = { runtime: 6, sidecar: 4 };

function run(command, commandArgs, cwd = root, env = undefined) {
  console.log(`\n$ ${command} ${commandArgs.join(" ")}`);
  const started = Date.now();
  const result = spawnSync(command, commandArgs, {
    cwd, stdio: "inherit", shell: process.platform === "win32", env: env ? { ...process.env, ...env } : undefined,
  });
  return { code: result.status ?? 1, seconds: Math.round((Date.now() - started) / 1000) };
}

/** `uv run --no-sync` in the project's shared environment (see the header), or plain `uv run`
 *  (uv's per-checkout .venv) when this checkout has its own .venv or the shared one is unusable.
 *  Called when the step is reached (an earlier failure skips the setup too), once per project. A
 *  fallback .venv this run creates is removed when the run ends, pass or fail (AGENTS.md: no
 *  task-created environment stays behind); one the checkout already had is left alone. */
const environments = new Map();
const fallbackVenvs = [];
function pythonStep(project, commandArgs) {
  if (!environments.has(project)) {
    let environment = null;
    if (!existsSync(join(root, project, ".venv"))) {
      environment = sharedEnvironmentPath(root, project);
      if (!dryRun) {
        try {
          environment = ensureSharedEnvironment(root, project).environment;
        } catch (error) {
          console.log(`${project}：${error.message}，改用本工作树 .venv（跑完删除）`);
          environment = null;
          fallbackVenvs.push(join(root, project, ".venv"));
        }
      }
    }
    environments.set(project, environment);
  }
  const environment = environments.get(project);
  return environment ? { args: ["run", "--no-sync", ...commandArgs], env: sharedRunEnv(root, project, environment) }
    : { args: ["run", ...commandArgs] };
}

const selection = planChecks(changed, { root });
const pick = (area) => selection.full || selection[area];
const summary = [];
let failed = false;

if (changed.length === 0) {
  console.log(`test:changed 汇总：相对 ${base} 没有改动，未运行测试 → exit 0`);
  process.exit(0);
}

const steps = [];
if (pick("client")) {
  const vitestArgs = ["--workspace", "@ai-trader/client", "run", "test", "--", "--bail=1", "--passWithNoTests"];
  if (!all && !selection.full) vitestArgs.push(`--changed=${base}`);
  steps.push(["client", all || selection.full ? "全量" : `vitest --changed ${base}`, "npm", vitestArgs]);
}
for (const area of ["runtime", "sidecar"]) {
  if (!pick(area)) continue;
  const { project, tests } = PYTHON[area];
  const workers = WORKERS[area];
  const targets = all || selection.full ? null : selection.python[area].tests;
  if (targets && targets.length === 0) {
    summary.push(`${area} 无受影响测试`);
    continue;
  }
  const files = targets ?? [tests];
  const parallel = targets && targets.length <= 3 ? ["-p", "no:xdist"] : ["-n", String(workers)];
  steps.push([area, targets ? `${targets.length} 个测试文件` : "全量", "uv", ["--project", project, ...PROJECT_EXTRAS[project],
    "pytest", ...files, ...parallel, "-x", "--durations=15", "--durations-min=2"], root, project]);
}
if (selection.full) {
  // The release tooling's Node tests (scripts/ changes select full).
  steps.push(["release", "全量", "npm", ["run", "test:release"]]);
}
if (pick("workspace")) {
  steps.push(["workspace", "全量", "uv", ["--project", "../services/trader-runtime", "pytest", "-x"], join(root, "workspace"),
    "services/trader-runtime"]);
}

for (const [area, scope, command, stepArgs, cwd, python] of steps) {
  if (failed) {
    summary.push(`${area} 未运行（前一项失败）`);
    continue;
  }
  // A Python step's arguments become `uv run …` once its environment is settled, here and not earlier.
  const { args: commandArgs, env } = python ? pythonStep(python, stepArgs) : { args: stepArgs };
  if (dryRun) {
    console.log(`$ ${env ? `UV_PROJECT_ENVIRONMENT=${env.UV_PROJECT_ENVIRONMENT} ` : ""}${command} ${commandArgs.join(" ")}`);
    summary.push(`${area} 未运行（--dry-run，${scope}）`);
    continue;
  }
  const { code, seconds } = run(command, commandArgs, cwd, env);
  summary.push(`${area} ${code === 0 ? "通过" : `失败 exit=${code}`}（${scope}，${seconds}s）`);
  failed = code !== 0;
}
for (const venv of fallbackVenvs) {
  rmSync(venv, { recursive: true, force: true });
  summary.push(`已删除临时 ${venv.slice(root.length)}`);
}
if (steps.length === 0) summary.push(`只有文档改动（${changed.length} 个文件），无测试可跑`);

console.log(`\ntest:changed 汇总（基准 ${base}，${changed.length} 个改动文件）：${summary.join("｜")} → exit ${failed ? 1 : 0}`);
process.exit(failed ? 1 : 0);


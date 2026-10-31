import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const repository = "kingcharleslzy-ai/ai-trader";
function output(command, args) {
  const result = spawnSync(command, args, { cwd: root, encoding: "utf8" });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`release_check_failed:${command}:${result.stderr.trim()}`);
  return result.stdout.trim();
}
const api = (path) => JSON.parse(output("gh", ["api", `repos/${repository}/${path}`]));

export function assertCleanReleaseSource(expectedCommit, { allowDirty = false } = {}) {
  const commit = output("git", ["rev-parse", "HEAD"]);
  if (expectedCommit && expectedCommit !== commit) throw new Error("release_source_changed");
  if (output("git", ["status", "--porcelain", "--untracked-files=normal"])) {
    // Only the explicit --verify-only run (which publishes nothing) may carry uncommitted release
    // tooling; the caller passes the flag, nothing is read from the environment.
    if (allowDirty) { console.error("release_source_dirty (allowed: verify-only)"); return commit; }
    throw new Error("release_source_dirty");
  }
  return commit;
}

// Require substantive verification, not GitHub's artifact-storage availability.
// Missing/skipped checks fail closed; upload failures remain visible to the caller.
// A check is a step name, or an array of names of which one must have passed (a renamed step keeps
// the runs recorded under its old name releasable).
export const requiredChecks = {
  // Lint was one step until 2026-10-05 (split into lint:web / lint:python for
  // change-aware PRs); each array accepts the pre-split aliases so a main run of
  // either shape satisfies the release check. On main both steps always run.
  "verify-linux": [
    ["Run npm run lint:web", "Lint (web and python in parallel)", "Run npm run lint"],
    ["Run npm run lint:python", "Lint (web and python in parallel)", "Run npm run lint"],
    "Run npm run test:unit",
    "Build qilinshuzhi.com web candidate",
  ],
  "verify-mac": ["Run npm run test:runtime", "Run npm run test:sidecar"],
  desktop: ["Run cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml", ["Verify signed app and DMG bundles", "Verify unsigned app and DMG bundles"], ["Smoke packaged CLI and MCP configuration", "Smoke packaged local Fake runtime and SSE"], "Smoke desktop single instance and child cleanup", "Prepare macOS candidate"],
  "windows-desktop": [["Smoke packaged CLI and MCP configuration", "Smoke packaged local Fake runtime and SSE"], "Smoke Windows single instance and child cleanup", "Smoke NSIS install, launch and uninstall", "Prepare Windows candidate checksum"],
  "cloud-compose": ["Run uv run --project services/trader-runtime python scripts/smoke_cloud_stack.py", "Verify production cloud UI at seven viewports"],
};
const uploads = new Set(["Upload web candidate", "Upload internal macOS build", "Upload internal Windows candidate"]);

// The installer's provenance names the attempt in which the Windows job actually ran. A later
// `gh run rerun --failed` of another job copies untouched jobs into the new attempt (new job id,
// new run_attempt, same started_at), so the binding is: that attempt's Windows job succeeded and the
// latest Windows job is the same execution (not started again since).
export function assertWindowsProvenanceAttempt(latestJob, attemptJob) {
  if (!attemptJob || attemptJob.conclusion !== "success" || !attemptJob.started_at
      || attemptJob.started_at !== latestJob.started_at) {
    throw new Error("release_windows_ci_attempt_mismatch");
  }
}

/** Every required check has a passed step; a check given as an array accepts any of its names. */
export function assertRequiredSteps(name, steps, checks) {
  for (const check of checks) {
    const names = Array.isArray(check) ? check : [check];
    if (!steps.some((step) => names.includes(step.name) && step.conclusion === "success")) {
      throw new Error(`release_ci_check_not_passed:${name}:${names[0]}`);
    }
  }
}

export function checkReleaseSource({ runId, runAttempt, allowDirty = false } = {}) {
  const commit = assertCleanReleaseSource(undefined, { allowDirty });
  output("git", ["fetch", "origin", "main", "--no-tags"]);
  output("git", ["merge-base", "--is-ancestor", commit, "origin/main"]);
  const run = runId
    ? api(`actions/runs/${runId}`)
    : api(`actions/workflows/ci.yml/runs?head_sha=${commit}&branch=main&per_page=1`).workflow_runs[0];
  // A manual `gh workflow run ci.yml --ref main` run is as good as the push run: since 2026-10-12 the
  // push run skips windows-desktop when the desktop package did not change, and the manual run is how
  // the release commit gets desktop and windows-desktop green in one run.
  if (!run || run.head_sha !== commit || run.head_branch !== "main" || !["push", "workflow_dispatch"].includes(run.event)
      || run.path !== ".github/workflows/ci.yml" || run.status !== "completed") {
    throw new Error("release_ci_source_or_completion_mismatch");
  }
  // A job-only retry increments the run attempt but leaves successful jobs and
  // their artifacts intact. Select the latest execution of each job, then bind
  // the Windows provenance to its own execution rather than the entire run.
  const { jobs } = api(`actions/runs/${run.id}/jobs?filter=latest&per_page=100`);
  const uploadFailures = [];
  // Runs recorded before the verify split (≤ 1.5.6) carry one "verify" job with the combined steps.
  const legacyVerify = jobs.some((item) => item.name === "verify") && !jobs.some((item) => item.name === "verify-linux");
  const required = legacyVerify
    ? { verify: ["Run npm run test", "Run npm run test:e2e", "Build qilinshuzhi.com web candidate"], desktop: requiredChecks.desktop.filter((step) => step !== "Prepare macOS candidate"), "windows-desktop": requiredChecks["windows-desktop"], "cloud-compose": requiredChecks["cloud-compose"] }
    : requiredChecks;
  for (const [name, checks] of Object.entries(required)) {
    const job = jobs.find((item) => item.name === name);
    if (job && job.conclusion === "skipped") {
      throw new Error(`release_ci_job_skipped:${name}: 该提交的 main CI 运行没有跑 ${name}（桌面无改动时被跳过）。gh workflow run ci.yml --ref main 只构建 main 当前 tip：先核对 ${commit.slice(0, 12)} 是否仍是 origin/main 的 tip——是则执行该命令，等该运行全绿后重试；main 已前进则这条命令不会为该提交补跑，须改发当前 tip`);
    }
    if (!job || job.status !== "completed" || job.head_sha !== commit) {
      throw new Error(`release_ci_job_missing:${name}`);
    }
    if (name === "windows-desktop" && runAttempt) {
      const { jobs: attemptJobs } = api(`actions/runs/${run.id}/attempts/${Number(runAttempt)}/jobs?per_page=100`);
      assertWindowsProvenanceAttempt(job, attemptJobs.find((item) => item.name === name));
    }
    assertRequiredSteps(name, job.steps, checks);
    for (const step of job.steps) {
      if (["failure", "cancelled", "timed_out"].includes(step.conclusion)) {
        if (!uploads.has(step.name)) throw new Error(`release_ci_step_failed:${name}:${step.name}`);
        uploadFailures.push(step.name);
      }
    }
  }
  assertCleanReleaseSource(commit, { allowDirty });
  return { commit, runId: run.id, runAttempt: run.run_attempt, uploadFailures };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.stdout.write(`${JSON.stringify(checkReleaseSource())}\n`);
}

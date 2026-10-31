// Decides whether a main push / manual run must run the `windows-desktop` CI job
// (owner decision 2026-10-12: it only runs when the desktop package can have changed, so the 4090D
// is not busy for unrelated pushes). Runs on the self-hosted linux-ci runner in the `desktop-changes`
// job; writes `desktop=true|false` to $GITHUB_OUTPUT.
//
//   EVENT_NAME=push BEFORE=<previous main sha> SHA=<new main sha> node scripts/ci_desktop_changes.mjs
//
// Fail-safe: anything that is not a plain, readable `before..sha` diff means "run" — a manual
// dispatch, a missing/all-zero/unreachable `before` (new branch, force push), a failing git call.
import { spawnSync } from "node:child_process";
import { appendFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

/** Files that feed the windows-desktop job's package build or smoke tests, or the job itself.
 *  The version-prep PR always touches apps/desktop (package.json, tauri.conf.json, Cargo.*). */
export function isDesktopPath(path) {
  return path.startsWith("apps/desktop/")
    || path === ".github/workflows/ci.yml"
    || path === "scripts/build_desktop_app.mjs"
    || path === "scripts/build_desktop_sidecars.py"
    || /^scripts\/desktop_[^/]+$/.test(path)
    || /^scripts\/smoke_desktop_[^/]+$/.test(path)
    || path === "scripts/smoke_windows_installer.ps1";
}

/** @returns {{ run: boolean, reason: string }} */
export function decideDesktop({ eventName, before, changedFiles }) {
  if (eventName === "workflow_dispatch") return { run: true, reason: "manual dispatch" };
  if (!before || /^0+$/.test(before)) return { run: true, reason: "no previous commit" };
  if (!changedFiles) return { run: true, reason: "previous commit unreachable or diff unreadable" };
  const hit = changedFiles.filter(isDesktopPath);
  return hit.length
    ? { run: true, reason: `desktop files changed: ${hit.slice(0, 5).join(", ")}${hit.length > 5 ? ` (+${hit.length - 5})` : ""}` }
    : { run: false, reason: `no desktop files among ${changedFiles.length} changed` };
}

function git(args) {
  const result = spawnSync("git", args, { encoding: "utf8" });
  return result.status === 0 ? result.stdout : null;
}

function changedBetween(before, sha) {
  if (git(["cat-file", "-e", `${before}^{commit}`]) === null) {
    git(["fetch", "--no-tags", "origin", before]);
    if (git(["cat-file", "-e", `${before}^{commit}`]) === null) return null;
  }
  const out = git(["diff", "--name-only", "--no-renames", before, sha]);
  return out === null ? null : out.split("\n").filter(Boolean);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const { EVENT_NAME: eventName = "", BEFORE: before = "", SHA: sha = "HEAD" } = process.env;
  const needsDiff = eventName !== "workflow_dispatch" && before && !/^0+$/.test(before);
  const decision = decideDesktop({ eventName, before, changedFiles: needsDiff ? changedBetween(before, sha) : [] });
  console.log(`windows-desktop: ${decision.run ? "run" : "skip"} (${decision.reason})`);
  if (process.env.GITHUB_OUTPUT) appendFileSync(process.env.GITHUB_OUTPUT, `desktop=${decision.run}\n`);
}

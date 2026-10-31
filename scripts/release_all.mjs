// One entry for the whole release of a main commit, run from a clean checkout of that commit:
//
//   npm run release:all -- --sha <full-sha> [--scope auto|full] [--desktop] [--base <sha>] [--base-runtime|--base-web|--base-desktop <sha>]
//                           [--notes "..."] [--dry-run] [--skip-github-release] [--skip-public-repo]
//
// Scope (owner decisions 2026-09-18: a Runtime-only fix must not rerun the desktop release; client
// changes are tried on the web app first and the desktop package ships when a version-prep PR lands):
//   --scope auto (default): each segment diffs ITS OWN live commit against the target — runtime from
//   healthz.commit, web from the web app's version.json, desktop from the latest GitHub Release's
//   target. runtime/web run when their paths changed; desktop runs only when the version number
//   (the 15-file version set) changed since the desktop base — client changes without a version bump
//   are listed as deferred and ride the next version. --desktop forces the desktop segment, --scope
//   full runs every segment. --base overrides all three bases, --base-<segment> one.
//
// Sequence (each segment logs to ~/ai-trader-release-artifacts/release-<version>/<segment>.log):
//   0. preflight: HEAD == sha, clean tree, sha on origin/main, configuration preflight, the
//      Windows installer + provenance for this version and commit present (one from another
//      commit is set aside; a missing one is fetched over SSH/scp from the 4090D CI host, where
//      the windows-desktop job keeps it as D:\ci-candidates\ai-trader\main-<sha>)
//   1. in parallel: desktop update (release:desktop:update — CI macOS candidate + real signature,
//      strict render smoke, aliyun + OSS), cloud Runtime deploy (deploy/vps/deploy-release.sh),
//      web (deploy/web/deploy-web.sh)
//   2. GitHub Release from the CHANGELOG section with the assets pulled back from OSS; the same checked
//      dmg/exe/SHA256SUMS then go to the public repository kingcharleslzy-ai/sentetrader with a refreshed
//      snapshot of the official page (scripts/sync_public_repo.mjs; reported in the summary, never fails the run)
//   3. readback summary with timings
// A failed segment fails the whole run; the summary lists what completed and what did not. No
// segment is rolled back here: the deploy script rolls the Runtime back itself, and published
// update sources are never withdrawn automatically (owner rule).
import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { createWriteStream, existsSync, mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { ensureSharedEnvironment, sharedEnvironmentPath, sharedEnvironmentReady } from "./shared_python_env.mjs";
import { syncPublicRepo } from "./sync_public_repo.mjs";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const ossBase = "https://ai-trader-updates-hz.oss-cn-hangzhou.aliyuncs.com";
const installerSuffixes = ["", ".provenance.json", ".sha256"];

const SEGMENTS = ["desktop-update", "runtime-deploy", "web-deploy"];
/** Files that mean "the desktop package changed" although they live outside apps/desktop. */
const DESKTOP_VERSION_FILES = new Set(["package.json", "package-lock.json", "apps/client/package.json", "apps/desktop/package.json",
  "packages/ui-events/package.json", "apps/client/public/version.json"]);
const SEGMENT_RULES = [
  ["runtime-deploy", (file) => file.startsWith("services/") || file.startsWith("deploy/vps/")],
  ["web-deploy", (file) => file.startsWith("apps/client/") || file.startsWith("packages/") || file.startsWith("deploy/web/")],
  ["desktop-update", (file) => file.startsWith("apps/desktop/") || file.startsWith("apps/client/") || file.startsWith("packages/")
    || /^scripts\/publish[^/]*$/.test(file) || DESKTOP_VERSION_FILES.has(file)],
];

/** Which segments a set of changed files calls for, with the files that decided each one.
 *  Anything else (docs, CHANGELOG, tests, CI, other scripts) releases nothing. With `filesBySegment`
 *  each segment judges its own diff (its live commit..target); the plain form judges one diff for all.
 *  The desktop segment needs `desktopVersionChanged` (a version-prep PR since the desktop base) or
 *  `forceDesktop`; desktop-relevant files without a new version are returned as `deferred`. */
export function releaseScope(files, filesBySegment = undefined, { desktopVersionChanged = false, forceDesktop = false } = {}) {
  const reasons = Object.fromEntries(SEGMENTS.map((name) => [name, []]));
  const seen = new Set();
  for (const [name, matches] of SEGMENT_RULES) {
    for (const file of filesBySegment ? filesBySegment[name] ?? [] : files) { seen.add(file); if (matches(file)) reasons[name].push(file); }
  }
  const deferred = [];
  if (!desktopVersionChanged && !forceDesktop) deferred.push(...reasons["desktop-update"].splice(0));
  else if (forceDesktop && !reasons["desktop-update"].length) reasons["desktop-update"].push("--desktop");
  const segments = SEGMENTS.filter((name) => reasons[name].length);
  const all = filesBySegment ? [...seen] : files;
  return { segments, reasons, deferred, ignored: all.filter((file) => !SEGMENT_RULES.some(([, matches]) => matches(file))) };
}

/** The version number the 15-file set carried at a commit (package.json is its anchor). */
function versionAt(commit, output) {
  return JSON.parse(output("git", ["show", `${commit}:package.json`])).version;
}

/** Copies the remote candidate directory into `localDir` (it lands as `localDir/<basename(remoteDir)>`).
 *  Returns `{ status, stderr }` like spawnSync; exit 255 means the host could not be reached. */
export function scpCopyDirectory({ host, remoteDir, localDir, extraArgs = [] }) {
  const result = spawnSync("scp", ["-r", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=10",
    ...extraArgs, `${host}:${remoteDir}`, localDir], { encoding: "utf8", timeout: 600000 });
  return { status: result.status ?? 255, stderr: `${result.stderr || ""}${result.error ? String(result.error) : ""}` };
}

/** Fetches the Windows installer set the `windows-desktop` CI job kept on the 4090D as
 *  `<remoteRoot>/main-<sha>/` (installer, `.sha256`, `.provenance.json`) into `destDir`. It tries the
 *  direct route first and then, when `jumpHost` is set, the same host through that jump host (scp -J:
 *  the jump host only relays TCP, the 4090D still authenticates this machine's key). The set is copied
 *  to a staging directory and checked there — provenance commit/version/artifact, installer SHA256
 *  against provenance and against the `.sha256` file — and only then moved into `destDir`, so a short
 *  or tampered transfer never lands next to a good release set. */
export function fetchWindowsCandidate({ sha, version, destDir, host, remoteRoot, jumpHost = "", copy = scpCopyDirectory, log = console.log }) {
  const installerName = `AI Trader_${version}_x64-setup.exe`;
  const remoteDir = `${remoteRoot.replace(/\/+$/, "")}/main-${sha}`;
  mkdirSync(destDir, { recursive: true });
  const staging = mkdtempSync(join(destDir, ".fetch-"));
  try {
    const routes = [{ name: "direct", extraArgs: [] }];
    if (jumpHost && jumpHost !== "none") routes.push({ name: `via ${jumpHost}`, extraArgs: ["-J", jumpHost] });
    let copied = false;
    const failures = [];
    for (const route of routes) {
      const result = copy({ host, remoteDir, localDir: staging, extraArgs: route.extraArgs });
      if (result.status === 0) { copied = true; log(`Windows candidate main-${sha.slice(0, 7)} fetched from ${host} (${route.name})`); break; }
      if (result.status !== 255 && /no such file|not found|cannot find/i.test(result.stderr)) {
        throw new Error(`release_all_windows_candidate_missing:main-${sha}: ${host} 上没有该提交的 Windows 候选（桌面无改动时 windows-desktop 被跳过，或候选超过 7 天已清理）。gh workflow run ci.yml --ref main 只构建 main 当前 tip：先核对 ${sha.slice(0, 12)} 是否仍是 origin/main 的 tip——是则执行该命令，等该运行全绿后重试；main 已前进则这条命令不会生成 ${sha.slice(0, 12)} 的候选，须改发当前 tip`);
      }
      failures.push(`${route.name}: exit ${result.status} ${result.stderr.trim().split("\n").slice(-1)[0] || ""}`.trim());
      if (result.status !== 255) break;
    }
    if (!copied) {
      throw new Error(`release_all_windows_candidate_unreachable:${host}: 取不到 4090D 上的 Windows 候选（${failures.join("; ")}）。确认 4090D 开机且 Tailscale 在线（ssh ${host} hostname）、候选目录 ${remoteDir} 存在；直连不通时可设 AI_TRADER_WINDOWS_JUMP_HOST（默认 mac-mini-cc，none 关闭）。不降低校验，不能用别处来源代替`);
    }
    const fetched = join(staging, `main-${sha}`);
    for (const suffix of installerSuffixes) {
      if (!existsSync(join(fetched, `${installerName}${suffix}`))) throw new Error(`release_all_windows_candidate_incomplete:${installerName}${suffix}`);
    }
    const installer = join(fetched, installerName);
    const provenance = JSON.parse(readFileSync(`${installer}.provenance.json`, "utf8"));
    if (provenance.commit !== sha || provenance.version !== version || provenance.artifact !== installerName) throw new Error("release_all_windows_provenance_mismatch");
    const digest = createHash("sha256").update(readFileSync(installer)).digest("hex");
    if (!provenance.sha256 || digest !== provenance.sha256) throw new Error("release_all_windows_installer_digest_mismatch");
    if (!readFileSync(`${installer}.sha256`, "utf8").startsWith(`${digest}  ${installerName}`)) throw new Error("release_all_windows_checksum_file_mismatch");
    for (const suffix of installerSuffixes) renameSync(join(fetched, `${installerName}${suffix}`), join(destDir, `${installerName}${suffix}`));
  } finally {
    rmSync(staging, { recursive: true, force: true });
  }
}

/** The Windows installer for this version and commit is in place: an installer left from another
 *  commit is moved to stale-<short>/, and a missing or incomplete set is fetched (`download()` puts the
 *  installer, its .provenance.json and .sha256 into the installer's directory — since 2026-10-15
 *  `fetchWindowsCandidate` from the 4090D CI host). */
export function ensureWindowsInstaller({ installer, sha, version, download, log = console.log }) {
  const provenancePath = `${installer}.provenance.json`;
  if (existsSync(provenancePath)) {
    const existing = JSON.parse(readFileSync(provenancePath, "utf8"));
    if (existing.commit !== sha) {
      const staleDir = join(dirname(installer), `stale-${String(existing.commit).slice(0, 7)}`);
      mkdirSync(staleDir, { recursive: true });
      for (const suffix of installerSuffixes) if (existsSync(`${installer}${suffix}`)) renameSync(`${installer}${suffix}`, join(staleDir, `${basename(installer)}${suffix}`));
      log(`Windows installer in ${dirname(installer)} was built from ${existing.commit.slice(0, 7)}, moved to ${staleDir}`);
    }
  }
  if (installerSuffixes.some((suffix) => !existsSync(`${installer}${suffix}`))) {
    log(`Windows installer for ${version} not in ${dirname(installer)}; fetching the CI candidate from the Windows host`);
    for (const suffix of installerSuffixes) rmSync(`${installer}${suffix}`, { force: true });
    download();
  }
  const provenance = JSON.parse(readFileSync(provenancePath, "utf8"));
  if (provenance.commit !== sha || provenance.version !== version) throw new Error("release_all_windows_provenance_mismatch");
  if (provenance.sha256 && createHash("sha256").update(readFileSync(installer)).digest("hex") !== provenance.sha256) {
    throw new Error("release_all_windows_installer_digest_mismatch");
  }
  return provenance;
}

/** GitHub refuses a Release body over 125000 characters (HTTP 422; v1.8.0's CHANGELOG section was
 *  ~150000 and had to be summarised by hand, #932 B6). */
export const GITHUB_RELEASE_BODY_LIMIT = 125000;

/** The GitHub Release notes for `version`: its CHANGELOG section. A section over `limit` keeps every
 *  subsection (要点, 维护记录, …) except 全部改动, which becomes its entry count and a link to the
 *  section in the tagged CHANGELOG.md — the one authoritative record. If that still does not fit,
 *  the notes are the link alone. */
export function releaseNotesFromChangelog(changelog, version, { limit = GITHUB_RELEASE_BODY_LIMIT, repo = "kingcharleslzy-ai/ai-trader" } = {}) {
  const start = changelog.indexOf(`## ${version} - `);
  if (start < 0) throw new Error("release_all_changelog_section_missing");
  const heading = changelog.slice(start + 3, changelog.indexOf("\n", start)).trim();
  const bodyStart = changelog.indexOf("\n", start) + 1;
  const next = changelog.indexOf("\n## ", bodyStart);
  const section = changelog.slice(bodyStart, next < 0 ? undefined : next).trim();
  // The returned body ends in a newline: it counts against the limit too.
  if (section.length < limit) return `${section}\n`;
  // GitHub's heading anchor: lower case, punctuation other than "-" dropped, spaces to "-".
  const anchor = heading.toLowerCase().replace(/[^\p{L}\p{N}\s-]/gu, "").replace(/\s/g, "-");
  const link = `[CHANGELOG.md ${version} 段](https://github.com/${repo}/blob/v${version}/CHANGELOG.md#${anchor})`;
  const parts = section.split(/\n(?=### )/);
  const summarised = parts.map((part) => {
    if (!part.startsWith("### 全部改动")) return part.trim();
    const entries = part.split("\n").filter((line) => line.startsWith("- ")).length;
    return `### 全部改动\n\n共 ${entries} 条，全文超出 GitHub Release 说明长度上限，见仓库 ${link}（唯一权威记录，本说明不另列）。`;
  }).join("\n\n");
  if (summarised.length < limit) return `${summarised}\n`;
  return `${version} 的说明超出 GitHub Release 长度上限，全文见仓库 ${link}（唯一权威记录）。\n`;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();

async function main() {
const args = process.argv.slice(2);
const flag = (name) => { const index = args.indexOf(name); return index >= 0 ? args[index + 1] : undefined; };
const has = (name) => args.includes(name);
const sha = flag("--sha") || "";
const dryRun = has("--dry-run");
const scopeMode = flag("--scope") || "auto";
const forceDesktop = has("--desktop");
if (!["auto", "full"].includes(scopeMode)) throw new Error("release_all_scope_invalid");
if (!/^[0-9a-f]{40}$/.test(sha)) throw new Error("release_all_sha_required");
const short = sha.slice(0, 7);
const version = JSON.parse(readFileSync(join(root, "package.json"), "utf8")).version;
const notes = flag("--notes") || `${version}：见 GitHub Release 说明。macOS 已 Apple 签名并公证；Windows 安装包未签名，首次打开需在系统提示里允许。`;
const assetsDir = join(homedir(), "ai-trader-release-assets", version);
const logDir = join(homedir(), "ai-trader-release-artifacts", `release-${version}`);
mkdirSync(logDir, { recursive: true });
mkdirSync(assetsDir, { recursive: true });
const windowsInstaller = join(assetsDir, `AI Trader_${version}_x64-setup.exe`);
const macCandidate = join(homedir(), ".cache/ai-trader/desktop-candidates", `main-${sha}`);
const macBuildHost = process.env.AI_TRADER_MAC_BUILD_HOST || "mac-mini-cc";
// The windows-desktop CI job keeps its installer set on the 4090D under <root>/main-<sha>/ (OpenSSH
// path form /D:/...). The direct route is tried first, then the same host through the jump host.
const windowsBuildHost = process.env.AI_TRADER_WINDOWS_BUILD_HOST || "gpu-4090d";
const windowsCandidateRoot = process.env.AI_TRADER_WINDOWS_CANDIDATE_ROOT || "/D:/ci-candidates/ai-trader";
const windowsJumpHost = process.env.AI_TRADER_WINDOWS_JUMP_HOST ?? "mac-mini-cc";

function output(command, commandArgs) {
  const result = spawnSync(command, commandArgs, { cwd: root, encoding: "utf8", env: { ...process.env, CARGO_TARGET_DIR: process.env.CARGO_TARGET_DIR || join(homedir(), ".cache/ai-trader/cargo-target") } });
  if (result.status !== 0) throw new Error(`release_all_command_failed:${command} ${commandArgs.join(" ")}\n${result.stderr}`);
  return result.stdout.trim();
}

const timings = {};
const timed = async (name, work) => {
  const started = Date.now();
  try { return await work(); }
  finally { timings[name] = Math.round((Date.now() - started) / 1000); }
};

/** Run a command with its output captured to <logDir>/<name>.log; resolves with the exit code. */
function segment(name, command, commandArgs, env = {}) {
  const logPath = join(logDir, `${name}.log`);
  // In a dry run the deploy scripts run their own --dry-run (read-only plan); the desktop update
  // has no side-effect-free mode and is only printed.
  if (dryRun && !commandArgs.includes("--dry-run")) { console.log(`[dry-run:${name}] ${command} ${commandArgs.join(" ")}`); return Promise.resolve(0); }
  if (dryRun) console.log(`[dry-run:${name}] ${command} ${commandArgs.join(" ")} → ${logPath}`);
  return new Promise((resolvePromise) => {
    const log = createWriteStream(logPath);
    const child = spawn(command, commandArgs, { cwd: root, env: { ...process.env, CARGO_TARGET_DIR: process.env.CARGO_TARGET_DIR || join(homedir(), ".cache/ai-trader/cargo-target"), ...env } });
    child.stdout.pipe(log, { end: false });
    child.stderr.pipe(log, { end: false });
    child.on("exit", (code) => { log.end(); resolvePromise(code ?? 1); });
    child.on("error", () => { log.end(); resolvePromise(1); });
  });
}
const tail = (name, lines = 12) => {
  const logPath = join(logDir, `${name}.log`);
  return existsSync(logPath) ? readFileSync(logPath, "utf8").trim().split("\n").slice(-lines).join("\n") : "";
};

// --- 0. preflight -----------------------------------------------------------------------------
let scope = { mode: scopeMode, segments: SEGMENTS, bases: {}, reasons: {}, deferred: [], ignored: [] };
await timed("preflight", async () => {
  if (output("git", ["rev-parse", "HEAD"]) !== sha) throw new Error("release_all_head_mismatch");
  if (!dryRun) {
    if (output("git", ["status", "--porcelain", "--untracked-files=normal"])) throw new Error("release_all_tree_dirty");
    output("git", ["fetch", "origin", "main", "--no-tags"]);
    output("git", ["merge-base", "--is-ancestor", sha, "origin/main"]);
  }
  if (scopeMode === "auto") {
    // Each segment's base is what it currently serves: a Runtime hotfix moves only healthz.commit,
    // so a single base would hide the desktop/web changes made since their last release.
    const curl = (url) => { try { return output("curl", ["-s", "--noproxy", "*", "-m", "15", url]); } catch { return ""; } };
    const live = {
      "runtime-deploy": () => JSON.parse(curl("https://ai-runtime.qilinshuzhi.com:8443/healthz")).commit,
      "web-deploy": () => JSON.parse(curl("https://qilinshuzhi.com/trader/version.json")).commit,
      "desktop-update": () => output("gh", ["release", "view", output("gh", ["release", "list", "--limit", "1", "--json", "tagName", "-q", ".[0].tagName"]), "--json", "targetCommitish", "-q", ".targetCommitish"]),
    };
    const bases = {};
    for (const name of SEGMENTS) {
      let base = flag(`--base-${name.split("-")[0]}`) || flag("--base") || "";
      if (!base) { try { base = String(live[name]() || ""); } catch { base = ""; } }
      if (!/^[0-9a-f]{40}$/.test(base)) throw new Error(`release_all_scope_base_unknown:${name}: pass --base-${name.split("-")[0]} <sha>, --base <sha> or --scope full`);
      output("git", ["cat-file", "-e", `${base}^{commit}`]);
      bases[name] = base;
    }
    if (Object.values(bases).every((base) => base === sha)) throw new Error("release_all_scope_nothing_new: every surface already serves this commit");
    const filesBySegment = Object.fromEntries(SEGMENTS.map((name) => [name, bases[name] === sha ? [] : output("git", ["diff", "--name-only", `${bases[name]}..${sha}`]).split("\n").filter(Boolean)]));
    const desktopVersionChanged = bases["desktop-update"] !== sha && versionAt(bases["desktop-update"], output) !== version;
    const decided = releaseScope([], filesBySegment, { desktopVersionChanged, forceDesktop });
    scope = { mode: "auto", bases, ...decided };
    console.log(`scope auto → ${decided.segments.join(", ") || "nothing"}`);
    for (const name of SEGMENTS) {
      const hits = decided.reasons[name];
      const why = name === "desktop-update" ? (forceDesktop ? "--desktop" : desktopVersionChanged ? `version ${versionAt(bases[name], output)} → ${version}` : "version unchanged") : "";
      console.log(`  ${name}: live ${bases[name].slice(0, 7)}, ${filesBySegment[name].length} files changed since, ${hits.length ? `run — ${why ? `${why}; ` : ""}${hits.slice(0, 6).join(", ")}${hits.length > 6 ? ` (+${hits.length - 6})` : ""}` : `skip${why ? ` — ${why}` : ""}`}`);
    }
    if (decided.deferred.length) console.log(`  desktop has ${decided.deferred.length} unreleased file(s) but no new version — deferred to the next version prep: ${decided.deferred.slice(0, 6).join(", ")}${decided.deferred.length > 6 ? ` (+${decided.deferred.length - 6})` : ""}`);
    if (decided.ignored.length) console.log(`  not released by themselves: ${decided.ignored.slice(0, 8).join(", ")}${decided.ignored.length > 8 ? ` (+${decided.ignored.length - 8})` : ""}`);
    if (!decided.segments.length) throw new Error("release_all_scope_nothing_to_release");
  } else {
    console.log("scope full: all segments");
  }
  const desktop = scope.segments.includes("desktop-update");
  const preflight = JSON.parse(output("node", ["scripts/check_desktop_release.mjs", "--configuration-only"]));
  // The version set must be consistent for any segment; only a desktop release also requires it to
  // be a new version (the update sources refuse a republish). Runtime/web deploys assert their own
  // version and commit against the checked-out tree.
  if (preflight.status !== "ready" || preflight.version !== version) throw new Error("release_all_preflight_not_ready");
  if (desktop) {
    // prepare:sidecars packages the Runtime from the shared exact-lock environment; a version bump
    // changes services/trader-runtime/uv.lock, so its environment is usually new (#932 B7: the
    // 1.8.0 desktop segment failed on sidecar_exact_lock_environment_missing after Runtime and web
    // had shipped). Build it here, before any segment runs; a checkout's own .venv is used as is.
    const project = "services/trader-runtime";
    if (existsSync(join(root, project, ".venv"))) console.log(`${project}/.venv present: prepare:sidecars uses it`);
    else if (dryRun) console.log(`[dry-run] shared Python environment ${sharedEnvironmentPath(root, project)}: ${sharedEnvironmentReady(root, project) ? "ready" : "missing — the real run builds it from the lock"}`);
    else ensureSharedEnvironment(root, project);
  }
  if (desktop && !dryRun) {
    // The set the windows-desktop job of this exact commit kept on the 4090D. Since 2026-10-12 a push
    // without desktop changes skips that job, so there may be none; a manual `gh workflow run ci.yml
    // --ref main` run supplies it. Which run built it is bound later by the provenance run id/attempt
    // (check_release_source in release:desktop:update).
    ensureWindowsInstaller({ installer: windowsInstaller, sha, version, download: () => fetchWindowsCandidate({
      sha, version, destDir: dirname(windowsInstaller), host: windowsBuildHost, remoteRoot: windowsCandidateRoot, jumpHost: windowsJumpHost }) });
    mkdirSync(macCandidate, { recursive: true });
    output("rsync", ["-a", "--checksum", "--", `${macBuildHost}:.cache/ai-trader/desktop-candidates/main-${sha}/`, `${macCandidate}/`]);
  }
  // The strict render smoke inside release:desktop:update launches the new bundle; another
  // AI Trader instance (same bundle id) would swallow that launch through the single-instance lock.
  const running = spawnSync("pgrep", ["-f", "ai-trader-desktop$"], { encoding: "utf8" }).stdout.trim();
  if (desktop && running && !dryRun) throw new Error(`release_all_desktop_instance_running:${running.split("\n").join(",")}`);
  console.log(`preflight ok: ${version} @ ${short}${dryRun ? " (dry-run)" : ""}`);
});

// --- 1. the selected segments in parallel ---------------------------------------------------
const commands = {
  "desktop-update": ["npm", ["run", "release:desktop:update", "--", "--windows-installer", windowsInstaller, "--mac-candidate", macCandidate, "--notes", notes]],
  "runtime-deploy": ["sh", ["deploy/vps/deploy-release.sh", sha, ...(dryRun ? ["--dry-run"] : [])]],
  "web-deploy": ["sh", ["deploy/web/deploy-web.sh", sha, ...(dryRun ? ["--dry-run"] : [])]],
};
const status = Object.fromEntries(SEGMENTS.map((name) => [name, scope.segments.includes(name) ? false : "skipped"]));
await timed("parallel", () => Promise.all(scope.segments.map((name) =>
  timed(name, () => segment(name, ...commands[name])).then((code) => { status[name] = code === 0; }))));

// Which build path the Runtime deploy took (NAS registry / host itself / remote save), from the
// deploy script's first banner line — the summary must say it even though the detail lives in the log.
const buildPath = existsSync(join(logDir, "runtime-deploy.log"))
  ? (readFileSync(join(logDir, "runtime-deploy.log"), "utf8").split("\n").find((line) => line.startsWith("== release ")) || "").replace(/^== /, "")
  : "";

// --- 2. GitHub Release (after the update sources; only a desktop release has assets to publish) ---
let releaseUrl = "";
if (status["desktop-update"] === true && !has("--skip-github-release")) {
  await timed("github-release", async () => {
    if (dryRun) { console.log(`[dry-run:github-release] gh release create v${version} --target ${sha} + 5 assets from OSS`); return; }
    const published = tail("desktop-update", 40).split("\n").map((line) => { try { return JSON.parse(line); } catch { return null; } })
      .find((item) => item && item.status === "published");
    if (!published) throw new Error("release_all_release_id_missing");
    const files = [`AI-Trader_${version}_aarch64.app.tar.gz`, `AI-Trader_${version}_aarch64.dmg`, `AI-Trader_${version}_x64-setup.exe`, "release.json", "SHA256SUMS"];
    for (const file of files) output("curl", ["-sf", "--noproxy", "*", "-m", "900", "-o", join(assetsDir, file), `${ossBase}/releases/${published.releaseId}/${file}`]);
    const sums = readFileSync(join(assetsDir, "SHA256SUMS"), "utf8");
    for (const file of files.slice(0, 3)) {
      const digest = output("shasum", ["-a", "256", join(assetsDir, file)]).split(" ")[0];
      if (!sums.includes(`${digest}  ${file}`)) throw new Error(`release_all_asset_digest:${file}`);
    }
    const notesPath = join(logDir, "release-notes.md");
    writeFileSync(notesPath, releaseNotesFromChangelog(readFileSync(join(root, "CHANGELOG.md"), "utf8"), version));
    releaseUrl = output("gh", ["release", "create", `v${version}`, "--target", sha, "--title", `v${version}`, "--notes-file", notesPath, ...files.map((file) => join(assetsDir, file))]);
  });
}

// --- 2b. public repository: same checked installers + page snapshot; reported, never fails the release ---
let publicRepo;
if ((releaseUrl || (dryRun && status["desktop-update"] === true)) && !has("--skip-public-repo")) {
  publicRepo = await timed("public-repo", async () => syncPublicRepo({ version, assetsDir, changelog: readFileSync(join(root, "CHANGELOG.md"), "utf8"), dryRun }));
}

// --- 3. readback -----------------------------------------------------------------------------
const readback = {};
if (!dryRun) {
  const curl = (url) => { try { return output("curl", ["-s", "--noproxy", "*", "-m", "15", url]); } catch { return ""; } };
  try { const healthz = JSON.parse(curl("https://ai-runtime.qilinshuzhi.com:8443/healthz")); readback.healthz = `${healthz.version} ${healthz.commit?.slice(0, 7)} ${healthz.marketDataStatus} ${healthz.liveTrading}`; } catch { readback.healthz = "unreadable"; }
  for (const [name, url] of [["latest.qilinshuzhi", "https://qilinshuzhi.com/ai-trader-updates/latest.json"], ["latest.oss", `${ossBase}/latest.json`]]) {
    try { const manifest = JSON.parse(curl(url)); readback[name] = `${manifest.version} ${[...new Set(Object.values(manifest.platforms).map((item) => new URL(item.url).host))].join(",")}`; } catch { readback[name] = "unreadable"; }
  }
  try { readback.web = JSON.parse(curl("https://qilinshuzhi.com/trader/version.json")).commit?.slice(0, 7); } catch { readback.web = "unreadable"; }
}
const summary = { version, sha, dryRun, scope: { mode: scope.mode, bases: Object.fromEntries(Object.entries(scope.bases).map(([name, base]) => [name, base.slice(0, 7)])), segments: scope.segments, reasons: scope.reasons, deferred: scope.deferred },
  status, runtimeBuildPath: buildPath || undefined, releaseUrl: releaseUrl || undefined, publicRepo, readback, timingsSeconds: timings };
console.log(JSON.stringify(summary, null, 2));
const failed = Object.entries(status).filter(([, ok]) => ok === false).map(([name]) => name);
if (failed.length) {
  for (const name of failed) console.error(`--- ${name} failed; last lines of ${join(logDir, `${name}.log`)}:\n${tail(name)}`);
  console.error(`release_all_failed: ${failed.join(", ")}; completed: ${Object.entries(status).filter(([, ok]) => ok === true).map(([name]) => name).join(", ") || "none"}`);
  process.exit(1);
}
}

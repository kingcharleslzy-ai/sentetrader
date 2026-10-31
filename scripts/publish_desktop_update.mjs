import { createHash } from "node:crypto";
import { copyFileSync, cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { basename, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { ensureDesktopBuildLock } from "./desktop_build_lock.mjs";
import { assertCleanReleaseSource, checkReleaseSource } from "./check_release_source.mjs";
import { baseArtifacts, prepareDeltaArtifacts } from "./desktop_delta_artifacts.mjs";
import {
  buildDesktopUpdateManifest,
  parseDesktopUpdateArguments,
} from "./desktop_update_manifest.mjs";

await ensureDesktopBuildLock();
const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const remoteRoot = "/var/www/ai-trader-updates";
const clientPublicBase = "https://qilinshuzhi.com/ai-trader-updates";
// The OSS mirror is the primary download source for CN clients (the aliyun ECS host caps at
// ~1 Mbps egress). Manifest URLs point at it: 1.4+ clients rewrite the prefix to every source
// in desktop_updates.rs::SOURCES and race them; older clients download from it directly.
const ossPublicBase = "https://ai-trader-updates-hz.oss-cn-hangzhou.aliyuncs.com";
const ossBucket = "oss://ai-trader-updates-hz";
const ossEndpoint = "oss-cn-hangzhou-internal.aliyuncs.com";
// The former US mirror (zgo-la) was dropped with the Runtime move on 2026-09-16.
const updateTargets = [
  {
    host: "aliyun-qilinshuzhi",
    publicBase: clientPublicBase,
    mirrorLatestDownload: true,
  },
];
const privateKey = process.env.AI_TRADER_UPDATER_PRIVATE_KEY_PATH
  || join(homedir(), "Library/Application Support/com.kingcharles.ai-trader-release/updater.key");

function fail(message) {
  throw new Error(message);
}

const updateArguments = parseDesktopUpdateArguments(process.argv.slice(2));
const notes = updateArguments.notes;
const windowsInstallerSource = resolve(updateArguments.windowsInstaller);
const windowsProvenancePath = `${windowsInstallerSource}.provenance.json`;

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { cwd: root, stdio: "inherit", ...options });
  if (result.error) throw result.error;
  if (result.status !== 0) fail(`desktop_update_command_failed:${command}`);
}

function output(command, args) {
  const result = spawnSync(command, args, { cwd: root, encoding: "utf8" });
  if (result.error) throw result.error;
  if (result.status !== 0) fail(`desktop_update_command_failed:${command}`);
  return result.stdout.trim();
}

function sha256(path) {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

const packageJson = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
const tauri = JSON.parse(readFileSync(join(root, "apps/desktop/src-tauri/tauri.conf.json"), "utf8"));
const version = packageJson.version;
if (!/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(version)) fail("desktop_update_version_invalid");
if (tauri.version !== version) fail("desktop_update_source_version_mismatch");
if (!existsSync(privateKey) || !existsSync(`${privateKey}.pub`)) fail("desktop_update_signing_key_missing");
if (readFileSync(`${privateKey}.pub`, "utf8").trim() !== tauri.plugins?.updater?.pubkey?.trim()) {
  fail("desktop_update_public_key_mismatch");
}
if (tauri.plugins?.updater?.endpoints?.[0] !== `${clientPublicBase}/latest.json`) {
  fail("desktop_update_endpoint_mismatch");
}
const expectedWindowsInstaller = `AI Trader_${version}_x64-setup.exe`;
if (
  !existsSync(windowsInstallerSource)
  || basename(windowsInstallerSource) !== expectedWindowsInstaller
) fail("desktop_update_windows_installer_invalid");
if (!existsSync(windowsProvenancePath)) fail("desktop_update_windows_provenance_missing");
const sourceCommit = output("git", ["rev-parse", "HEAD"]);
const windowsDigest = sha256(windowsInstallerSource);
const windowsProvenance = JSON.parse(readFileSync(windowsProvenancePath, "utf8"));
if (
  windowsProvenance.schemaVersion !== 1
  || windowsProvenance.artifact !== basename(windowsInstallerSource)
  || windowsProvenance.version !== version
  || windowsProvenance.commit !== sourceCommit
  || windowsProvenance.sha256 !== windowsDigest
  || windowsProvenance.liveTrading !== "disabled"
  || !/^\d+$/.test(String(windowsProvenance.githubRunId || ""))
  || !/^\d+$/.test(String(windowsProvenance.githubRunAttempt || ""))
) fail("desktop_update_windows_provenance_mismatch");
const verification = checkReleaseSource({
  runId: windowsProvenance.githubRunId,
  runAttempt: windowsProvenance.githubRunAttempt,
  allowDirty: updateArguments.verifyOnly,
});
process.stdout.write(`${JSON.stringify({ status: "source_verified", ...verification })}\n`);

const cargoTargetRoot = process.env.CARGO_TARGET_DIR
  ? resolve(process.env.CARGO_TARGET_DIR)
  : join(homedir(), ".cache/ai-trader/cargo-target");
process.env.CARGO_TARGET_DIR = cargoTargetRoot;

// The macOS payload comes from the main CI desktop job when its candidate matches this release
// (same commit, same run as the Windows installer, hashes intact); only the signature is made
// here, with the real key. An explicit candidate must match or the release stops; only an
// implicit local candidate retains the existing local-build fallback.
function ciMacCandidate() {
  const candidateRoot = resolve(updateArguments.macCandidate
    || join(homedir(), ".cache/ai-trader/desktop-candidates", `main-${sourceCommit}`));
  const provenancePath = join(candidateRoot, "provenance.json");
  if (!existsSync(provenancePath)) return { reason: `mac_candidate_missing:${candidateRoot}` };
  const provenance = JSON.parse(readFileSync(provenancePath, "utf8"));
  const archive = join(candidateRoot, "macos", provenance.archive || "");
  const candidateDmg = join(candidateRoot, "dmg", provenance.dmg || "");
  const app = join(candidateRoot, "macos/AI Trader.app");
  const checks = [
    [provenance.schemaVersion === 1 && provenance.platform === "darwin-aarch64", "mac_candidate_schema"],
    [provenance.version === version, "mac_candidate_version"],
    [provenance.commit === sourceCommit, "mac_candidate_commit"],
    [String(provenance.githubRunId) === String(windowsProvenance.githubRunId), "mac_candidate_run_mismatch"],
    [provenance.liveTrading === "disabled", "mac_candidate_live_trading"],
    [existsSync(archive) && existsSync(candidateDmg) && existsSync(join(app, "Contents/Info.plist")), "mac_candidate_files_missing"],
    [basename(candidateDmg) === `AI Trader_${version}_aarch64.dmg`, "mac_candidate_dmg_name"],
  ];
  for (const [ok, reason] of checks) if (!ok) return { reason };
  if (sha256(archive) !== provenance.archiveSha256) return { reason: "mac_candidate_archive_digest" };
  if (sha256(candidateDmg) !== provenance.dmgSha256) return { reason: "mac_candidate_dmg_digest" };
  return { candidateRoot, provenance, archive, dmg: candidateDmg, app };
}

let bundleRoot;
const candidate = ciMacCandidate();
if (updateArguments.macCandidate && !candidate.candidateRoot) {
  fail(`desktop_update_required_mac_candidate_invalid:${candidate.reason}`);
}
if (candidate.candidateRoot) {
  // Work on a private copy so the candidate stays exactly what CI produced.
  bundleRoot = mkdtempSync(join(tmpdir(), "ai-trader-mac-candidate-"));
  mkdirSync(join(bundleRoot, "macos"));
  mkdirSync(join(bundleRoot, "dmg"));
  cpSync(candidate.app, join(bundleRoot, "macos/AI Trader.app"), { recursive: true, verbatimSymlinks: true });
  copyFileSync(candidate.archive, join(bundleRoot, "macos/AI Trader.app.tar.gz"));
  copyFileSync(candidate.dmg, join(bundleRoot, "dmg", basename(candidate.dmg)));
  run("npm", ["--workspace", "@ai-trader/desktop", "run", "tauri", "--",
    "signer", "sign", "-f", privateKey, "-p", "", join(bundleRoot, "macos/AI Trader.app.tar.gz")]);
  process.stdout.write(`${JSON.stringify({ status: "mac_candidate_reused", candidate: candidate.candidateRoot,
    githubRunId: candidate.provenance.githubRunId, githubRunAttempt: candidate.provenance.githubRunAttempt })}\n`);
} else {
  process.stdout.write(`${JSON.stringify({ status: "mac_candidate_unavailable", reason: candidate.reason, fallback: "build:desktop" })}\n`);
  bundleRoot = join(cargoTargetRoot, "release/bundle");
  rmSync(bundleRoot, { recursive: true, force: true });
  run("npm", ["--workspace", "@ai-trader/desktop", "run", "prepare:sidecars"]);
  run("npm", ["--workspace", "@ai-trader/desktop", "run", "build:update"], {
    env: {
      ...process.env,
      CI: process.env.CI || "true",
      AI_TRADER_BUILD_SHA: sourceCommit,
      TAURI_SIGNING_PRIVATE_KEY: privateKey,
      TAURI_SIGNING_PRIVATE_KEY_PASSWORD: ""
    }
  });
  // The frontend Tauri just embedded must be the desktop build (base "/"); a web candidate written
  // into apps/client/dist during the compile produces a window that loads nothing (1.5.3).
  const embeddedIndex = readFileSync(join(root, "apps/client/dist/index.html"), "utf8");
  if (embeddedIndex.includes("/trader/") || !embeddedIndex.includes('src="/assets/')) fail("desktop_update_frontend_base_invalid");
}
const appBundle = join(bundleRoot, "macos/AI Trader.app");
const updaterArtifact = join(bundleRoot, "macos/AI Trader.app.tar.gz");
const signaturePath = `${updaterArtifact}.sig`;
const dmg = join(bundleRoot, `dmg/AI Trader_${version}_aarch64.dmg`);
for (const path of [appBundle, updaterArtifact, signaturePath, dmg]) {
  if (!existsSync(path)) fail(`desktop_update_artifact_missing:${basename(path)}`);
}

// Fail closed on anything but a Developer-ID-signed, notarized and stapled macOS bundle: an
// ad-hoc candidate must never reach the update sources (owner decision 2026-09-21). Runs for
// the reused CI candidate and the local-build fallback alike, before --verify-only exits.
function assertMacNotarized() {
  const designated = spawnSync("/usr/bin/codesign", ["-dv", "--verbose=2", appBundle], { encoding: "utf8" });
  if (designated.status !== 0 || !/Authority=Developer ID Application/.test(`${designated.stderr}${designated.stdout}`)) {
    fail("desktop_update_mac_candidate_not_notarized");
  }
  if (candidate.candidateRoot
    && (candidate.provenance.codeSigning !== "developer-id" || candidate.provenance.notarized !== true)) {
    fail("desktop_update_mac_candidate_not_notarized");
  }
  for (const path of [appBundle, dmg]) {
    if (spawnSync("xcrun", ["stapler", "validate", path]).status !== 0) fail("desktop_update_mac_candidate_not_notarized");
  }
  // -vv: without it spctl prints nothing on success. Its verdict spans two lines ("<dmg>: accepted" then
  // "source=Notarized Developer ID"), so the check reads across the newline.
  const gatekeeper = spawnSync("/usr/sbin/spctl", ["-a", "-vv", "-t", "install", dmg], { encoding: "utf8" });
  if (gatekeeper.status !== 0 || !/\baccepted\b[\s\S]*source=Notarized Developer ID/.test(`${gatekeeper.stdout}${gatekeeper.stderr}`)) {
    fail("desktop_update_mac_candidate_not_notarized");
  }
}
assertMacNotarized();

function validateBundleVersion(path) {
  const plist = join(path, "Contents/Info.plist");
  if (!existsSync(plist)) fail("desktop_update_info_plist_missing");
  const shortVersion = output("/usr/bin/plutil", ["-extract", "CFBundleShortVersionString", "raw", plist]);
  const bundleVersion = output("/usr/bin/plutil", ["-extract", "CFBundleVersion", "raw", plist]);
  if (shortVersion !== version || bundleVersion !== version) {
    fail("desktop_update_bundle_version_mismatch");
  }
}

validateBundleVersion(appBundle);
// The bundle must render: launch it and wait for the frontend-ready signal (the 1.5.3 bundle
// embedded a web-base frontend, ran nothing and shipped). Other AI Trader instances must be closed.
run("python3", ["scripts/smoke_desktop_lifecycle.py"], { env: { ...process.env,
  AI_TRADER_BUILD_SHA: sourceCommit,
  AI_TRADER_DESKTOP_EXECUTABLE: join(appBundle, "Contents/MacOS/ai-trader-desktop"), AI_TRADER_DESKTOP_SMOKE_STRICT: "1" } });
const archiveInspection = mkdtempSync(join(tmpdir(), "ai-trader-update-inspect-"));
try {
  run("/usr/bin/tar", ["-xzf", updaterArtifact, "-C", archiveInspection]);
  validateBundleVersion(join(archiveInspection, "AI Trader.app"));
} finally {
  rmSync(archiveInspection, { recursive: true, force: true });
}

// --verify-only 1: everything up to here (source check, candidate reuse or build, real signature,
// version checks, strict render smoke) without touching any update source — the acceptance path.
if (updateArguments.verifyOnly) {
  process.stdout.write(`${JSON.stringify({ status: "verified_not_published", version, sourceCommit,
    macCandidate: candidate.candidateRoot || null, darwinSha256: sha256(updaterArtifact), dmgSha256: sha256(dmg), windowsSha256: windowsDigest })}\n`);
  if (candidate.candidateRoot) rmSync(bundleRoot, { recursive: true, force: true });
  process.exit(0);
}
const darwinDigest = sha256(updaterArtifact);
const releaseDigest = createHash("sha256")
  .update(`${darwinDigest}:${windowsDigest}`)
  .digest("hex");
const releaseId = `${version}-${releaseDigest.slice(0, 16)}`;
const archiveName = `AI-Trader_${version}_aarch64.app.tar.gz`;
const dmgName = `AI-Trader_${version}_aarch64.dmg`;
const windowsInstallerName = `AI-Trader_${version}_x64-setup.exe`;
const pubDate = new Date().toISOString();
const darwinSignature = readFileSync(signaturePath, "utf8").trim();

const staging = mkdtempSync(join(tmpdir(), "ai-trader-update-"));
const publishingHosts = [];
try {
  const stagedWindowsInstaller = join(staging, windowsInstallerName);
  copyFileSync(windowsInstallerSource, stagedWindowsInstaller);
  run("npm", [
    "--workspace", "@ai-trader/desktop", "run", "tauri", "--",
    "signer", "sign", "-f", privateKey, "-p", "", stagedWindowsInstaller,
  ]);
  const windowsSignaturePath = `${stagedWindowsInstaller}.sig`;
  if (!existsSync(windowsSignaturePath)) fail("desktop_update_windows_signature_missing");
  const windowsSignature = readFileSync(windowsSignaturePath, "utf8").trim();
  // The delta tool lives in the Tauri crate, whose build script insists on the packaged Runtime
  // sidecar being present; a fresh worktree that reused the CI candidate never ran prepare:sidecars.
  if (!existsSync(join(root, "apps/desktop/src-tauri/binaries/ai-trader-runtime-aarch64-apple-darwin"))) {
    run("npm", ["--workspace", "@ai-trader/desktop", "run", "prepare:sidecars"]);
  }
  run("cargo", ["build", "--release", "--manifest-path", "apps/desktop/src-tauri/Cargo.toml",
    "--features", "delta-tools", "--bin", "ai-trader-delta"]);
  const bases = baseArtifacts(join(homedir(), "ai-trader-release-assets"), version);
  const deltaOptions = { version, bases, staging, run,
    deltaTool: join(cargoTargetRoot, "release/ai-trader-delta"),
    sign: (path) => run("npm", ["--workspace", "@ai-trader/desktop", "run", "tauri", "--",
      "signer", "sign", "-f", privateKey, "-p", "", path]) };
  const macDelta = prepareDeltaArtifacts({ ...deltaOptions, platform: "darwin-aarch64", artifact: updaterArtifact });
  const windowsDelta = prepareDeltaArtifacts({ ...deltaOptions, platform: "windows-x86_64", artifact: stagedWindowsInstaller });
  const deltaFiles = [...macDelta.files, ...windowsDelta.files];
  const checksumsPath = join(staging, "SHA256SUMS");
  writeFileSync(
    checksumsPath,
    [
      `${darwinDigest}  ${archiveName}`,
      `${sha256(dmg)}  ${dmgName}`,
      `${windowsDigest}  ${windowsInstallerName}`,
      ...deltaFiles.map(path => `${sha256(path)}  ${basename(path)}`),
      "",
    ].join("\n"),
    { mode: 0o644 },
  );
  const stagedTargets = [];
  assertCleanReleaseSource(sourceCommit, { allowDirty: updateArguments.verifyOnly });
  for (const target of updateTargets) {
    const manifest = buildDesktopUpdateManifest({
      version,
      notes,
      pubDate,
      publicBase: ossPublicBase,
      releaseId,
      darwinArtifactName: archiveName,
      darwinSignature,
      windowsArtifactName: windowsInstallerName,
      windowsSignature,
    });
    const manifestPath = join(staging, `latest-${target.host}.json`);
    manifest.platforms["darwin-aarch64"].integrity = macDelta.integrity;
    manifest.platforms["windows-x86_64"].integrity = windowsDelta.integrity;
    // Unsigned copies are only retention hints; the client trusts the signed payload.
    manifest.platforms["darwin-aarch64"].patches = macDelta.patches;
    manifest.platforms["windows-x86_64"].patches = windowsDelta.patches;
    writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, { mode: 0o644 });
    const remoteStaging = `${remoteRoot}/.staging-${releaseId}-${process.pid}`;
    const remoteRelease = `${remoteRoot}/releases/${releaseId}`;
    run("ssh", [target.host, `install -d -m 0755 '${remoteStaging}' '${remoteRoot}/releases'; touch '${remoteRoot}/.publishing'`]);
    publishingHosts.push(target.host);
    run("scp", [updaterArtifact, `${target.host}:${remoteStaging}/${archiveName}`]);
    run("scp", [dmg, `${target.host}:${remoteStaging}/${dmgName}`]);
    run("scp", [stagedWindowsInstaller, `${target.host}:${remoteStaging}/${windowsInstallerName}`]);
    for (const path of deltaFiles) run("scp", [path, `${target.host}:${remoteStaging}/${basename(path)}`]);
    run("scp", [manifestPath, `${target.host}:${remoteStaging}/release.json`]);
    run("scp", [checksumsPath, `${target.host}:${remoteStaging}/SHA256SUMS`]);
    stagedTargets.push({ ...target, remoteStaging, remoteRelease });
  }

  // Release files reach OSS before any latest.json points at them; they are still in the
  // staging directory at this point (the rename into releases/ happens below).
  syncOssRelease(releaseId, stagedTargets.find((target) => target.host === "aliyun-qilinshuzhi").remoteStaging);
  for (const target of stagedTargets) {
    const latestDownload = target.mirrorLatestDownload
      ? `; install -d -m 0755 '${remoteRoot}/latest'; cp '${target.remoteRelease}/${dmgName}' '${remoteRoot}/latest/.AI-Trader_latest_aarch64.dmg-${releaseId}'; chmod 0644 '${remoteRoot}/latest/.AI-Trader_latest_aarch64.dmg-${releaseId}'; mv '${remoteRoot}/latest/.AI-Trader_latest_aarch64.dmg-${releaseId}' '${remoteRoot}/latest/AI-Trader_latest_aarch64.dmg'; cp '${target.remoteRelease}/${windowsInstallerName}' '${remoteRoot}/latest/.AI-Trader_latest_x64-setup.exe-${releaseId}'; chmod 0644 '${remoteRoot}/latest/.AI-Trader_latest_x64-setup.exe-${releaseId}'; mv '${remoteRoot}/latest/.AI-Trader_latest_x64-setup.exe-${releaseId}' '${remoteRoot}/latest/AI-Trader_latest_x64-setup.exe'; cp '${target.remoteRelease}/SHA256SUMS' '${remoteRoot}/latest/.SHA256SUMS-${releaseId}'; chmod 0644 '${remoteRoot}/latest/.SHA256SUMS-${releaseId}'; mv '${remoteRoot}/latest/.SHA256SUMS-${releaseId}' '${remoteRoot}/latest/SHA256SUMS'`
      : "";
    run("ssh", [target.host, `set -eu; if [ -d '${target.remoteRelease}' ]; then rm -rf '${target.remoteStaging}'; else mv '${target.remoteStaging}' '${target.remoteRelease}'; fi; cp '${target.remoteRelease}/release.json' '${remoteRoot}/.latest-${releaseId}.json'; chmod 0644 '${remoteRoot}/.latest-${releaseId}.json'; mv '${remoteRoot}/.latest-${releaseId}.json' '${remoteRoot}/latest.json'${latestDownload}`]);
  }

  syncOssLatest(remoteRoot);
  assertMutablePathsNotCached();
} finally {
  for (const host of publishingHosts) {
    run("ssh", [host, `rm -f '${remoteRoot}/.publishing'; if [ -f /opt/ai-trader/maintenance/maintain_update_host.py ]; then systemctl start --no-block ai-trader-maintenance.service; fi`]);
  }
  rmSync(staging, { recursive: true, force: true });
  if (candidate.candidateRoot) rmSync(bundleRoot, { recursive: true, force: true });
}

// OSS keys come from the local Keychain entry and reach the aliyun host only through the ssh
// session's stdin (#715): in the ssh argv they were visible to `ps` on both machines, and keys
// containing quotes broke the command string. `read -r` takes each line verbatim, so the key
// bytes never need shell escaping and never land on disk in either place.
function ossCopy(source, destination, cacheControl, recursive) {
  if (process.env.AI_TRADER_SKIP_OSS === "1") return;
  const keyResult = spawnSync("security", ["find-generic-password", "-a", process.env.USER, "-s", "ai-trader-aliyun-ak", "-w"], { encoding: "utf8" });
  const raw = keyResult.status === 0 ? keyResult.stdout.trim() : "";
  const separator = raw.indexOf(":");
  if (!raw || separator <= 0) fail("desktop_update_oss_key_missing");
  const remote = [
    "set -eu",
    "IFS= read -r oss_key_id",
    "IFS= read -r oss_key_secret",
    'export ALIBABA_CLOUD_ACCESS_KEY_ID="$oss_key_id" ALIBABA_CLOUD_ACCESS_KEY_SECRET="$oss_key_secret" ALIBABA_CLOUD_REGION_ID=cn-hangzhou',
    `aliyun oss cp '${source}' '${ossBucket}/${destination}' --endpoint ${ossEndpoint} --meta Cache-Control:${cacheControl} ${recursive ? "--recursive" : ""} -f`,
  ].join("\n");
  const copied = spawnSync("ssh", ["aliyun-qilinshuzhi", remote], {
    cwd: root,
    stdio: ["pipe", "inherit", "inherit"],
    input: `${raw.slice(0, separator)}\n${raw.slice(separator + 1)}\n`,
  });
  if (copied.error) throw copied.error;
  if (copied.status !== 0) fail(`desktop_update_oss_copy_failed:${destination}`);
}

function syncOssRelease(releaseId, stagedReleaseDir) {
  ossCopy(`${stagedReleaseDir}/`, `releases/${releaseId}/`, "public,max-age=31536000", true);
}

// latest/ and latest.json are mutable paths: a browser or proxy that cached the previous
// installer for a year kept handing out 1.5.1 after 1.5.4 shipped (09-16). Only releases/<id>/
// is immutable and may be cached long-term.
function syncOssLatest(remoteRoot) {
  ossCopy(`${remoteRoot}/latest/`, "latest/", "no-cache", true);
  ossCopy(`${remoteRoot}/latest.json`, "latest.json", "no-cache", false);
}

function assertMutablePathsNotCached() {
  if (process.env.AI_TRADER_SKIP_OSS === "1") return;
  for (const path of ["latest.json", `latest/${dmgName.replace(version, "latest")}`, `latest/${windowsInstallerName.replace(version, "latest")}`, "latest/SHA256SUMS"]) {
    const headers = spawnSync("curl", ["-sI", "--noproxy", "*", "-m", "20", `${ossPublicBase}/${path}`], { encoding: "utf8" }).stdout || "";
    const control = /^cache-control:\s*(.+)$/im.exec(headers)?.[1]?.trim().toLowerCase() || "";
    if (control !== "no-cache") fail(`desktop_update_latest_cache_control_invalid:${path}:${control || "missing"}`);
  }
}

process.stdout.write(`${JSON.stringify({
  status: "published",
  version,
  releaseId,
  endpoint: `${clientPublicBase}/latest.json`,
  mirrors: updateTargets.map((target) => `${target.publicBase}/latest.json`),
  liveTrading: "disabled",
})}\n`);

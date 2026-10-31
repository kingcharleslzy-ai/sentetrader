import assert from "node:assert/strict";
import test from "node:test";
import { existsSync, readFileSync } from "node:fs";
import { validateMt5BridgeReadOnly, validateSigningEnvironment } from "./check_desktop_release.mjs";
import {
  buildDesktopUpdateManifest,
  parseDesktopUpdateArguments,
} from "./desktop_update_manifest.mjs";

test("desktop updater requires Windows artifacts and publishes both platform targets", () => {
  assert.throws(
    () => parseDesktopUpdateArguments(["--notes", "fixture"]),
    /desktop_update_windows_artifacts_required/,
  );
  assert.deepEqual(
    parseDesktopUpdateArguments([
      "--windows-installer", "AI Trader_1.0.1_x64-setup.exe",
      "--notes", "fixture",
    ]),
    {
      notes: "fixture",
      windowsInstaller: "AI Trader_1.0.1_x64-setup.exe",
      macCandidate: undefined,
      verifyOnly: false,
    },
  );
  // The release machine can point at a CI candidate and stop before publishing.
  assert.deepEqual(
    parseDesktopUpdateArguments(["--windows-installer", "w.exe", "--mac-candidate", "/tmp/c", "--verify-only", "1"]),
    { notes: parseDesktopUpdateArguments(["--windows-installer", "w.exe"]).notes, windowsInstaller: "w.exe", macCandidate: "/tmp/c", verifyOnly: true },
  );
  const manifest = buildDesktopUpdateManifest({
    version: "1.0.1",
    notes: "fixture",
    pubDate: "2026-09-01T00:00:00.000Z",
    publicBase: "https://updates.example.test/ai-trader",
    releaseId: "1.0.1-fixture",
    darwinArtifactName: "AI-Trader_1.0.1_aarch64.app.tar.gz",
    darwinSignature: "darwin-signature",
    windowsArtifactName: "AI-Trader_1.0.1_x64-setup.exe",
    windowsSignature: "windows-signature",
  });
  assert.deepEqual(Object.keys(manifest.platforms), ["darwin-aarch64", "windows-x86_64"]);
  assert.match(manifest.platforms["windows-x86_64"].url, /-setup\.exe$/);
  assert.equal(manifest.platforms["windows-x86_64"].signature, "windows-signature");
});

test("a configured signing identity must be Developer ID with full credentials; without one ad-hoc stays allowed", () => {
  // Local builds carry no Apple credentials; they stay ad-hoc and the preflight does not fail.
  assert.doesNotThrow(() => validateSigningEnvironment({}));
  const complete = {
    APPLE_CERTIFICATE: "fixture",
    APPLE_CERTIFICATE_PASSWORD: "fixture",
    APPLE_SIGNING_IDENTITY: "Developer ID Application: Fixture",
    APPLE_ID: "fixture@example.test",
    APPLE_PASSWORD: "fixture",
    APPLE_TEAM_ID: "FIXTURETEAM",
  };
  assert.doesNotThrow(() => validateSigningEnvironment(complete));
  // An identity without the rest of the credentials fails closed, and "-" is not a Developer ID.
  assert.throws(
    () => validateSigningEnvironment({ APPLE_SIGNING_IDENTITY: complete.APPLE_SIGNING_IDENTITY }),
    /desktop_signing_environment_missing/,
  );
  assert.throws(
    () => validateSigningEnvironment({ ...complete, APPLE_SIGNING_IDENTITY: "-" }),
    /desktop_signing_identity_must_be_developer_id/,
  );
  assert.throws(
    () => validateSigningEnvironment({ ...complete, APPLE_SIGNING_IDENTITY: "Mac Developer: Fixture" }),
    /desktop_signing_identity_must_be_developer_id/,
  );
  assert.doesNotThrow(() => validateSigningEnvironment(
    { ...complete, VITE_CLOUD_RUNTIME_URL: "https://runtime.example.test" },
  ));
  assert.throws(
    () => validateSigningEnvironment(
      { ...complete, VITE_CLOUD_RUNTIME_URL: "http://runtime.example.test" },
    ),
    /desktop_cloud_endpoint_must_be_https/,
  );
});

test("Windows provenance binds to the attempt that ran the job, also when a later rerun carried the job over", async () => {
  const { assertWindowsProvenanceAttempt } = await import("./check_release_source.mjs");
  const ran = { id: 1, run_attempt: 1, conclusion: "success", started_at: "2026-09-17T10:13:54Z" };
  // `gh run rerun --failed` of another job: the same execution reappears with a new id and attempt.
  const carried = { ...ran, id: 2, run_attempt: 2 };
  assertWindowsProvenanceAttempt(ran, ran);
  assertWindowsProvenanceAttempt(carried, ran);
  // Re-executed after the provenance was written, failed in that attempt, or that attempt has no job.
  assert.throws(() => assertWindowsProvenanceAttempt({ ...ran, id: 3, run_attempt: 3, started_at: "2026-09-17T10:38:51Z" }, ran), /release_windows_ci_attempt_mismatch/);
  assert.throws(() => assertWindowsProvenanceAttempt(ran, { ...ran, conclusion: "failure" }), /release_windows_ci_attempt_mismatch/);
  assert.throws(() => assertWindowsProvenanceAttempt(ran, undefined), /release_windows_ci_attempt_mismatch/);
});

test("release:all sets aside a Windows installer built from another commit and downloads the current one", async (t) => {
  const { mkdtempSync, existsSync, readFileSync, writeFileSync, readdirSync, rmSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const { createHash } = await import("node:crypto");
  const { ensureWindowsInstaller } = await import("./release_all.mjs");
  const dir = mkdtempSync(join(tmpdir(), "release-all-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const installer = join(dir, "AI Trader_1.5.7_x64-setup.exe");
  const sha = "a".repeat(40);
  const old = "b".repeat(40);
  const write = (commit, version = "1.5.7", digest = createHash("sha256").update(`exe ${commit}`).digest("hex")) => {
    writeFileSync(installer, `exe ${commit}`);
    writeFileSync(`${installer}.provenance.json`, JSON.stringify({ commit, version, sha256: digest }));
    writeFileSync(`${installer}.sha256`, "sum");
  };
  let downloads = 0;
  const download = () => { downloads += 1; write(sha); };
  const logs = [];
  // Stale installer from the previous SHA: moved to stale-<short>/, then the artifact downloaded once.
  write(old);
  assert.equal(ensureWindowsInstaller({ installer, sha, version: "1.5.7", download, log: (line) => logs.push(line) }).commit, sha);
  assert.equal(downloads, 1);
  assert.equal(readFileSync(join(dir, `stale-${old.slice(0, 7)}`, "AI Trader_1.5.7_x64-setup.exe"), "utf8"), `exe ${old}`);
  assert.equal(readdirSync(join(dir, `stale-${old.slice(0, 7)}`)).length, 3);
  assert.equal(readFileSync(installer, "utf8"), `exe ${sha}`);
  assert.match(logs[0], /built from bbbbbbb/);
  // Current installer present: nothing downloaded, nothing moved.
  ensureWindowsInstaller({ installer, sha, version: "1.5.7", download, log: () => {} });
  assert.equal(downloads, 1);
  assert.equal(existsSync(join(dir, `stale-${sha.slice(0, 7)}`)), false);
  // Right commit, wrong version: the release must not continue.
  write(sha, "1.5.6");
  assert.throws(() => ensureWindowsInstaller({ installer, sha, version: "1.5.7", download, log: () => {} }), /release_all_windows_provenance_mismatch/);
  // A companion file missing: the whole set is downloaded again.
  write(sha);
  rmSync(`${installer}.sha256`);
  ensureWindowsInstaller({ installer, sha, version: "1.5.7", download, log: () => {} });
  assert.equal(downloads, 2);
  // Installer bytes that do not match the provenance digest stop the release.
  write(sha, "1.5.7", "0".repeat(64));
  assert.throws(() => ensureWindowsInstaller({ installer, sha, version: "1.5.7", download, log: () => {} }), /release_all_windows_installer_digest_mismatch/);
});

test("release:all --scope auto: runtime/web by paths, desktop only when the version number changed", async () => {
  const { releaseScope } = await import("./release_all.mjs");
  // A Runtime hotfix (source, tests, docs, CHANGELOG): only the cloud deploy.
  const runtimeOnly = releaseScope(["services/trader-runtime/src/trader_runtime/agent_api.py", "services/trader-runtime/tests/test_agent_chat.py", "CHANGELOG.md", "docs/FUTURES_AGENT_CAPABILITY_V1.md"]);
  assert.deepEqual(runtimeOnly.segments, ["runtime-deploy"]);
  assert.deepEqual(runtimeOnly.ignored, ["CHANGELOG.md", "docs/FUTURES_AGENT_CAPABILITY_V1.md"]);
  // A client change without a version bump is tried on the web app; the desktop package waits and says so.
  const client = releaseScope(["apps/client/src/components/VoiceOrb.tsx", "packages/ui-events/src/index.ts"]);
  assert.deepEqual(client.segments, ["web-deploy"]);
  assert.deepEqual(client.deferred, ["apps/client/src/components/VoiceOrb.tsx", "packages/ui-events/src/index.ts"]);
  assert.deepEqual(client.reasons["desktop-update"], []);
  // The same change after a version-prep PR merged: desktop ships it (with the version set) alongside web.
  const bumped = releaseScope(["apps/client/src/components/VoiceOrb.tsx", "package.json", "package-lock.json", "apps/client/public/version.json"], undefined, { desktopVersionChanged: true });
  assert.deepEqual(bumped.segments, ["desktop-update", "web-deploy"]);
  assert.deepEqual(bumped.deferred, []);
  assert.deepEqual(bumped.reasons["desktop-update"], ["apps/client/src/components/VoiceOrb.tsx", "package.json", "package-lock.json", "apps/client/public/version.json"]);
  // --desktop forces the segment even with nothing desktop-relevant changed.
  const forced = releaseScope(["services/trader-runtime/src/trader_runtime/agent_api.py"], undefined, { forceDesktop: true });
  assert.deepEqual(forced.segments, ["desktop-update", "runtime-deploy"]);
  assert.deepEqual(forced.reasons["desktop-update"], ["--desktop"]);
  // Web deploy tooling alone is web-only; the desktop shell alone is deferred until a version bump.
  assert.deepEqual(releaseScope(["deploy/web/deploy-web.sh"]).segments, ["web-deploy"]);
  const shell = releaseScope(["apps/desktop/src-tauri/src/lib.rs", "scripts/publish_desktop_update.mjs"]);
  assert.deepEqual(shell.segments, []);
  assert.equal(shell.deferred.length, 2);
  // Per-segment bases: after a Runtime hotfix the Runtime is ahead (its diff to the target is empty) while
  // desktop and web still serve the previous release — web ships the client change, desktop waits for a version.
  const staggered = releaseScope([], {
    "runtime-deploy": [],
    "web-deploy": ["services/trader-runtime/src/trader_runtime/agent_api.py", "apps/client/src/components/VoiceOrb.tsx", "CHANGELOG.md"],
    "desktop-update": ["services/trader-runtime/src/trader_runtime/agent_api.py", "apps/client/src/components/VoiceOrb.tsx", "CHANGELOG.md"],
  });
  assert.deepEqual(staggered.segments, ["web-deploy"]);
  assert.deepEqual(staggered.reasons["runtime-deploy"], []);
  assert.deepEqual(staggered.deferred, ["apps/client/src/components/VoiceOrb.tsx"]);
  assert.deepEqual(staggered.ignored, ["CHANGELOG.md"]);
  // Nothing releasable: docs, CI, other scripts.
  const none = releaseScope([".github/workflows/ci.yml", "scripts/release_all.mjs", "README.md"]);
  assert.deepEqual(none.segments, []);
  assert.equal(none.ignored.length, 3);
});

test("release source check requires verify-linux's split lint steps and still accepts the old single name", async () => {
  const { assertRequiredSteps, requiredChecks } = await import("./check_release_source.mjs");
  const passed = (...names) => names.map((name) => ({ name, conclusion: "success" }));
  const current = passed("Run npm run lint:web", "Run npm run lint:python", "Build qilinshuzhi.com web candidate", "Run npm run test:unit", "Run npm run test:release");
  assert.doesNotThrow(() => assertRequiredSteps("verify-linux", current, requiredChecks["verify-linux"]));
  // Runs recorded before the 2026-10-05 split stay releasable via the aliases.
  const previous = passed("Run npm run lint", "Run npm run build", "Build qilinshuzhi.com web candidate", "Run npm run test:unit");
  assert.doesNotThrow(() => assertRequiredSteps("verify-linux", previous, requiredChecks["verify-linux"]));
  const older = passed("Lint (web and python in parallel)", "Build qilinshuzhi.com web candidate", "Run npm run test:unit");
  assert.doesNotThrow(() => assertRequiredSteps("verify-linux", older, requiredChecks["verify-linux"]));
  // A lint step that failed, or is missing, fails closed under the current name.
  const failedLint = [{ name: "Run npm run lint:python", conclusion: "failure" }, ...passed("Run npm run lint:web", "Build qilinshuzhi.com web candidate", "Run npm run test:unit")];
  assert.throws(() => assertRequiredSteps("verify-linux", failedLint, requiredChecks["verify-linux"]), /release_ci_check_not_passed:verify-linux:Run npm run lint:python/);
  assert.throws(() => assertRequiredSteps("verify-linux", passed("Run npm run test:unit"), requiredChecks["verify-linux"]), /release_ci_check_not_passed/);
});

// The skipped-job error must carry the same rebuild instructions as release_all's candidate-missing
// error (#932): the dispatch command builds only the current main tip, so the sha is checked first.
test("release_ci_job_skipped tells which tip the dispatch run would build", async () => {
  const source = readFileSync(new URL("./check_release_source.mjs", import.meta.url), "utf8");
  const message = source.match(/release_ci_job_skipped:\$\{name\}:[^`]+/)?.[0];
  assert.ok(message, "release_ci_job_skipped message not found");
  assert.match(message, /gh workflow run ci\.yml --ref main 只构建 main 当前 tip：先核对 \$\{commit\.slice\(0, 12\)\} 是否仍是 origin\/main 的 tip——是则执行该命令，等该运行全绿后重试；main 已前进则这条命令不会为该提交补跑，须改发当前 tip/);
});

// Renaming the packaged-CLI check must preserve releases from either workflow generation.
test("release source accepts both packaged CLI smoke labels and rejects missing checks", async () => {
  const { assertRequiredSteps, requiredChecks } = await import("./check_release_source.mjs");
  for (const job of ["desktop", "windows-desktop"]) {
    for (const label of ["Smoke packaged CLI and MCP configuration", "Smoke packaged local Fake runtime and SSE"]) {
      const steps = requiredChecks[job].map((check) => ({ name: Array.isArray(check) ? check[0] : check, conclusion: "success" }));
      steps.find((step) => step.name === "Smoke packaged CLI and MCP configuration").name = label;
      assert.doesNotThrow(() => assertRequiredSteps(job, steps, requiredChecks[job]));
      assert.throws(() => assertRequiredSteps(job, steps.filter((step) => step.name !== label), requiredChecks[job]), /release_ci_check_not_passed/);
    }
  }
});


test("desktop OSS copy sends AccessKey secrets over ssh stdin, never in the ssh argv (#715)", () => {
  const publishSource = readFileSync(new URL("./publish_desktop_update.mjs", import.meta.url), "utf8");
  // ossCopy lives inside the top-level publisher script, which cannot be imported without
  // publishing; the flat function body is extracted verbatim and wired to stubbed seams.
  const start = publishSource.indexOf("function ossCopy");
  assert.ok(start >= 0, "ossCopy not found in publish_desktop_update.mjs");
  const end = publishSource.indexOf("\n}\n", start) + 2;
  const secret = "fx'secret\"with$x\\back";
  const calls = [];
  const spawnSyncStub = (command, args, options = {}) => {
    calls.push({ command, args, input: options.input ?? null });
    if (command === "security") return { status: 0, stdout: `fixture-ak-id:${secret}\n`, stderr: "" };
    return { status: 0, stdout: "", stderr: "" };
  };
  const fail = (message) => { throw new Error(message); };
  const ossCopy = new Function("spawnSync", "fail", "ossBucket", "ossEndpoint", "root",
    `${publishSource.slice(start, end)}\nreturn ossCopy;`)(spawnSyncStub, fail,
    "oss://fixture-bucket", "oss-fixture.example.test", "/fixture/root");
  ossCopy("/remote/latest/", "latest/", "no-cache", true);
  const ssh = calls.filter((call) => call.command === "ssh");
  assert.equal(ssh.length, 1);
  assert.equal(ssh[0].args[0], "aliyun-qilinshuzhi");
  const remote = ssh[0].args[1];
  // The key value must not appear anywhere in the process arguments (ps-visible on both ends).
  assert.ok(!remote.includes(secret));
  // The export names the stdin-read variable; no argument embeds a literal key value.
  assert.match(remote, /ALIBABA_CLOUD_ACCESS_KEY_SECRET="\$oss_key_secret"/);
  assert.ok(remote.startsWith("set -eu\nIFS= read -r oss_key_id\nIFS= read -r oss_key_secret\n"));
  assert.match(remote, /aliyun oss cp '\/remote\/latest\/' 'oss:\/\/fixture-bucket\/latest\/' --endpoint oss-fixture\.example\.test --meta Cache-Control:no-cache --recursive -f$/);
  // The credentials travel verbatim on stdin; quoting hazards in the key need no escaping.
  assert.equal(ssh[0].input, `fixture-ak-id\n${secret}\n`);
  // A single-file copy omits --recursive, and a failed copy fails the release with the target named.
  calls.length = 0;
  ossCopy("/remote/latest.json", "latest.json", "no-cache", false);
  const single = calls.find((call) => call.command === "ssh");
  assert.doesNotMatch(single.args[1], /--recursive/);
  assert.throws(() => {
    const failing = (command, args, options = {}) => {
      if (command === "security") return { status: 0, stdout: `fixture-ak-id:${secret}\n`, stderr: "" };
      return { status: 23, stdout: "", stderr: "oss error" };
    };
    const failingCopy = new Function("spawnSync", "fail", "ossBucket", "ossEndpoint", "root",
      `${publishSource.slice(start, end)}\nreturn ossCopy;`)(failing, fail,
      "oss://fixture-bucket", "oss-fixture.example.test", "/fixture/root");
    failingCopy("/remote/latest.json", "latest.json", "no-cache", false);
  }, /desktop_update_oss_copy_failed:latest\.json/);
  // Without a usable Keychain entry the publisher stops before any ssh call.
  calls.length = 0;
  const keyless = (command) => (command === "security" ? { status: 4430, stdout: "", stderr: "" } : { status: 0, stdout: "" });
  assert.throws(() => {
    const keylessCopy = new Function("spawnSync", "fail", "ossBucket", "ossEndpoint", "root",
      `${publishSource.slice(start, end)}\nreturn ossCopy;`)(keyless, fail,
      "oss://fixture-bucket", "oss-fixture.example.test", "/fixture/root");
    keylessCopy("/x", "y", "no-cache", false);
  }, /desktop_update_oss_key_missing/);
});

// The windows-desktop job runs only for desktop-relevant main pushes (owner 2026-10-12).
test("windows-desktop runs on a desktop change, a dispatch or an unreadable diff, and is skipped otherwise", async () => {
  const { decideDesktop, isDesktopPath } = await import("./ci_desktop_changes.mjs");
  const before = "a".repeat(40);
  const push = (changedFiles, over = {}) => decideDesktop({ eventName: "push", before, changedFiles, ...over }).run;
  // Ordinary main push: runtime/client/docs/other scripts do not build the Windows package.
  assert.equal(push(["services/trader-runtime/src/x.py", "apps/client/src/App.tsx", "docs/A.md", "scripts/release_all.mjs", "CHANGELOG.md"]), false);
  // Version-prep push: the 15-file set always touches apps/desktop.
  assert.equal(push(["package.json", "apps/desktop/package.json", "apps/desktop/src-tauri/tauri.conf.json", "CHANGELOG.md"]), true);
  assert.equal(push(["scripts/smoke_desktop_lifecycle.py"]), true);
  assert.equal(push(["scripts/smoke_windows_installer.ps1"]), true);
  assert.equal(push(["scripts/build_desktop_sidecars.py"]), true);
  assert.equal(push([".github/workflows/ci.yml"]), true);
  // Manual dispatch always runs, even with an empty diff.
  assert.equal(decideDesktop({ eventName: "workflow_dispatch", before: "", changedFiles: [] }).run, true);
  // Unusable `before` (new branch, force push, unreachable) or an unreadable diff: run.
  assert.equal(push([], { before: "0".repeat(40) }), true);
  assert.equal(push([], { before: "" }), true);
  assert.equal(push(null), true);
  assert.equal(isDesktopPath("apps/desktop-notes.md"), false);
  assert.equal(isDesktopPath("scripts/desktop_update_manifest.mjs"), true);
});

test("release:all fetches the Windows candidate from the 4090D by exact SHA and checks it before it lands", async (t) => {
  const { mkdtempSync, mkdirSync, existsSync, readFileSync, writeFileSync, readdirSync, rmSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const { createHash } = await import("node:crypto");
  const { fetchWindowsCandidate } = await import("./release_all.mjs");
  const root = mkdtempSync(join(tmpdir(), "windows-candidate-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const sha = "a".repeat(40);
  const name = "AI Trader_1.5.7_x64-setup.exe";
  const common = { sha, version: "1.5.7", host: "gpu-4090d", remoteRoot: "/D:/ci-candidates/ai-trader/", log: () => {} };
  // Writes what the CI job keeps: installer, .sha256 and .provenance.json under main-<sha>/ (mutate to break one).
  const candidate = (localDir, mutate = () => {}) => {
    const dir = join(localDir, `main-${sha}`);
    mkdirSync(dir, { recursive: true });
    const bytes = Buffer.from("installer bytes");
    const digest = createHash("sha256").update(bytes).digest("hex");
    const files = {
      [name]: bytes,
      [`${name}.sha256`]: `${digest}  ${name}\n`,
      [`${name}.provenance.json`]: JSON.stringify({ schemaVersion: 1, artifact: name, version: "1.5.7", commit: sha, sha256: digest, githubRunId: "1", githubRunAttempt: "1" }),
    };
    mutate(files, digest);
    for (const [file, content] of Object.entries(files)) if (content !== null) writeFileSync(join(dir, file), content);
  };
  const dest = () => mkdtempSync(join(root, "dest-"));
  const ok = { status: 0, stderr: "" };

  // Direct route works: the set lands in destDir with the exact name, the staging dir is gone, the SHA is in the remote path.
  let destDir = dest();
  const calls = [];
  fetchWindowsCandidate({ ...common, destDir, copy: (call) => { calls.push(call); candidate(call.localDir); return ok; } });
  assert.deepEqual(readdirSync(destDir).sort(), [name, `${name}.provenance.json`, `${name}.sha256`].sort());
  assert.equal(calls.length, 1);
  assert.equal(calls[0].remoteDir, `/D:/ci-candidates/ai-trader/main-${sha}`);
  assert.deepEqual(calls[0].extraArgs, []);

  // Direct route unreachable (255), the jump route works: second attempt carries -J.
  destDir = dest();
  const routes = [];
  fetchWindowsCandidate({ ...common, destDir, jumpHost: "mac-mini-cc", copy: (call) => {
    routes.push(call.extraArgs.join(" "));
    if (!call.extraArgs.length) return { status: 255, stderr: "ssh: connect to host 100.86.62.70 port 22: Operation timed out" };
    candidate(call.localDir); return ok;
  } });
  assert.deepEqual(routes, ["", "-J mac-mini-cc"]);
  assert.equal(existsSync(join(destDir, name)), true);

  // Both routes unreachable: stop with an actionable error, nothing lands.
  destDir = dest();
  assert.throws(() => fetchWindowsCandidate({ ...common, destDir, jumpHost: "mac-mini-cc", copy: () => ({ status: 255, stderr: "Connection timed out" }) }),
    /release_all_windows_candidate_unreachable:gpu-4090d.*direct: exit 255.*via mac-mini-cc: exit 255/s);
  assert.deepEqual(readdirSync(destDir), []);
  // Jump disabled: only the direct route is tried.
  let attempts = 0;
  assert.throws(() => fetchWindowsCandidate({ ...common, destDir, jumpHost: "none", copy: () => { attempts += 1; return { status: 255, stderr: "x" }; } }), /candidate_unreachable/);
  assert.equal(attempts, 1);

  // The 4090D answers but has no candidate for this commit (job skipped, or pruned after 7 days): no second route, tells how to rebuild.
  destDir = dest();
  attempts = 0;
  assert.throws(() => fetchWindowsCandidate({ ...common, destDir, jumpHost: "mac-mini-cc", copy: () => { attempts += 1; return { status: 1, stderr: "scp: realpath /D:/ci-candidates/ai-trader/main-x: No such file" }; } }),
    /release_all_windows_candidate_missing:main-a+.*只构建 main 当前 tip.*核对 a{12} 是否仍是 origin\/main 的 tip/);
  assert.equal(attempts, 1);

  // A companion file missing, a different commit/version, a bad installer digest and a bad .sha256 all stop the release and leave destDir empty.
  const broken = [
    [(files) => { files[`${name}.sha256`] = null; }, /release_all_windows_candidate_incomplete/],
    [(files) => { files[`${name}.provenance.json`] = null; }, /release_all_windows_candidate_incomplete/],
    [(files) => { files[name] = null; }, /release_all_windows_candidate_incomplete/],
    [(files, digest) => { files[`${name}.provenance.json`] = JSON.stringify({ artifact: name, version: "1.5.7", commit: "b".repeat(40), sha256: digest }); }, /release_all_windows_provenance_mismatch/],
    [(files, digest) => { files[`${name}.provenance.json`] = JSON.stringify({ artifact: name, version: "1.5.6", commit: sha, sha256: digest }); }, /release_all_windows_provenance_mismatch/],
    [(files) => { files[name] = Buffer.from("tampered"); }, /release_all_windows_installer_digest_mismatch/],
    [(files, digest) => { files[`${name}.sha256`] = `${"0".repeat(64)}  ${name}\n`; }, /release_all_windows_checksum_file_mismatch/],
  ];
  for (const [mutate, expected] of broken) {
    destDir = dest();
    assert.throws(() => fetchWindowsCandidate({ ...common, destDir, copy: (call) => { candidate(call.localDir, mutate); return ok; } }), expected);
    assert.deepEqual(readdirSync(destDir), [], `nothing may land after ${expected}`);
  }
  // A good set already in destDir survives a failed second fetch.
  destDir = dest();
  fetchWindowsCandidate({ ...common, destDir, copy: (call) => { candidate(call.localDir); return ok; } });
  assert.throws(() => fetchWindowsCandidate({ ...common, destDir, copy: (call) => { candidate(call.localDir, (files) => { files[name] = Buffer.from("tampered"); }); return ok; } }), /digest_mismatch/);
  assert.equal(readFileSync(join(destDir, name), "utf8"), "installer bytes");
});

test("the MT5 market-data bridge may not name a trading call or an account login", async () => {
  const { mkdtempSync, mkdirSync, writeFileSync, rmSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  // The shipped bridge passes.
  assert.doesNotThrow(() => validateMt5BridgeReadOnly());
  const root = mkdtempSync(join(tmpdir(), "mt5-bridge-check-"));
  try {
    mkdirSync(join(root, "services/mt5-bridge/src/mt5_bridge"), { recursive: true });
    mkdirSync(join(root, "services/mt5-bridge/deploy/windows"), { recursive: true });
    mkdirSync(join(root, "services/mt5-bridge/src/.venv"), { recursive: true });
    const source = join(root, "services/mt5-bridge/src/mt5_bridge/terminal.py");
    writeFileSync(source, "def quotes(mt5):\n    return mt5.symbol_info_tick('GCEZ26')\n");
    // A uv environment holds the official package itself, which defines the trading calls.
    writeFileSync(join(root, "services/mt5-bridge/src/.venv/MetaTrader5.py"), "def order_send(request): ...\n");
    assert.doesNotThrow(() => validateMt5BridgeReadOnly(root));
    for (const [file, text] of [
      [source, "mt5.order_send(request)\n"],
      [source, "mt5.order_check(request)\n"],
      [source, "mt5.order_calc_margin(0, 'GCEZ26', 1.0, 4000.0)\n"],
      [source, "mt5.login(123, password='x')\n"],
      [join(root, "services/mt5-bridge/deploy/windows/supervise.ps1"), "python -c 'import MetaTrader5 as m; m.order_send({})'\n"],
    ]) {
      writeFileSync(source, "pass\n");
      writeFileSync(file, text);
      assert.throws(() => validateMt5BridgeReadOnly(root), /mt5_bridge_trading_symbol_forbidden/, text);
      writeFileSync(file, "pass\n");
    }
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("release:all keeps a long CHANGELOG section under GitHub's Release body limit (#932 B6)", async () => {
  const { releaseNotesFromChangelog, GITHUB_RELEASE_BODY_LIMIT } = await import("./release_all.mjs");
  assert.equal(GITHUB_RELEASE_BODY_LIMIT, 125000);
  const section = (entries) => ["## Unreleased", "", "## 1.9.0 - 2026-10-30", "", "范围：说明。", "", "### 要点", "", "- 要点一",
    "", "### 全部改动", "", ...Array.from({ length: entries }, (_, index) => `- 第 ${index} 条改动\n  - 细节`), "",
    "### 维护记录", "", "执行：某代理。", "", "## 1.8.0 - 2026-10-17", "", "- 旧版本"].join("\n");
  // Within the limit: the section itself, unchanged.
  assert.equal(releaseNotesFromChangelog(section(3), "1.9.0"),
    "范围：说明。\n\n### 要点\n\n- 要点一\n\n### 全部改动\n\n- 第 0 条改动\n  - 细节\n- 第 1 条改动\n  - 细节\n- 第 2 条改动\n  - 细节\n\n### 维护记录\n\n执行：某代理。\n");
  // Over it: 全部改动 becomes its count and the tagged CHANGELOG link; 要点 and 维护记录 stay.
  const long = releaseNotesFromChangelog(section(40), "1.9.0", { limit: 400 });
  assert.ok(long.includes("### 要点\n\n- 要点一") && long.includes("### 维护记录\n\n执行：某代理。"));
  assert.match(long, /### 全部改动\n\n共 40 条，全文超出 GitHub Release 说明长度上限，见仓库 \[CHANGELOG\.md 1\.9\.0 段\]\(https:\/\/github\.com\/kingcharleslzy-ai\/ai-trader\/blob\/v1\.9\.0\/CHANGELOG\.md#190---2026-10-30\)/);
  assert.ok(!long.includes("第 0 条改动") && !long.includes("旧版本"));
  // The real 1.8.0 section (~150000 characters, HTTP 422 on 10-17) now fits, with the anchor GitHub uses.
  // The public export carries no CHANGELOG (copy.bara.sky).
  const changelog = new URL("../CHANGELOG.md", import.meta.url);
  if (existsSync(changelog)) {
    const real = releaseNotesFromChangelog(readFileSync(changelog, "utf8"), "1.8.0");
    assert.ok(real.length <= GITHUB_RELEASE_BODY_LIMIT);
    assert.ok(real.includes("CHANGELOG.md#180---2026-10-17"));
  }
  // The trailing newline counts (GitHub measures the whole body): a section of exactly `limit`
  // characters is summarised, one character shorter passes as it is.
  const exact = section(3).split("## 1.9.0 - 2026-10-30\n")[1].split("\n## 1.8.0")[0].trim().length;
  assert.ok(releaseNotesFromChangelog(section(3), "1.9.0", { limit: exact + 1 }).length <= exact + 1);
  assert.ok(releaseNotesFromChangelog(section(3), "1.9.0", { limit: exact + 1 }).includes("第 0 条改动"));
  assert.ok(!releaseNotesFromChangelog(section(3), "1.9.0", { limit: exact }).includes("第 0 条改动"));
  // Even the summary too long: the link alone.
  assert.match(releaseNotesFromChangelog(section(3), "1.9.0", { limit: 20 }), /^1\.9\.0 的说明超出 GitHub Release 长度上限，全文见仓库 \[CHANGELOG\.md 1\.9\.0 段\]/);
  assert.throws(() => releaseNotesFromChangelog(section(1), "2.0.0"), /release_all_changelog_section_missing/);
});

test("shared exact-lock Python environment: built once per lock, marked, never one bound to a checkout (#932 B7/B11)", async (t) => {
  const { mkdtempSync, mkdirSync, writeFileSync, readFileSync: read, existsSync, rmSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const { ensureSharedEnvironment, sharedEnvironmentPath, sharedEnvironmentReady, LOCK_MARKER } = await import("./shared_python_env.mjs");
  const dir = mkdtempSync(join(tmpdir(), "shared-python-env-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const root = join(dir, "checkout");
  const deps = join(dir, "python-deps");
  const project = "services/trader-runtime";
  mkdirSync(join(root, project), { recursive: true });
  writeFileSync(join(root, project, "uv.lock"), "lock-v1\n");
  const calls = [];
  const sync = (command, args, options) => {
    calls.push({ command, args, environment: options.env.UV_PROJECT_ENVIRONMENT });
    mkdirSync(join(options.env.UV_PROJECT_ENVIRONMENT, "lib", "python3.12", "site-packages"), { recursive: true });
    writeFileSync(join(options.env.UV_PROJECT_ENVIRONMENT, "pyvenv.cfg"), "");
    return { status: 0 };
  };
  const environment = sharedEnvironmentPath(root, project, deps);
  assert.equal(sharedEnvironmentReady(root, project, deps), false);
  const first = ensureSharedEnvironment(root, project, { depsRoot: deps, sync, log: () => {} });
  assert.deepEqual([first.environment, first.created], [environment, true]);
  assert.deepEqual(calls[0].args, ["sync", "--frozen", "--no-install-project", "--quiet", "--project", join(root, project)]);
  assert.equal(calls[0].environment, environment);
  assert.equal(read(join(environment, LOCK_MARKER), "utf8"), "lock-v1\n");
  assert.equal(sharedEnvironmentReady(root, project, deps), true);
  assert.equal(ensureSharedEnvironment(root, project, { depsRoot: deps, sync, log: () => {} }).created, false);
  // A version bump changes the lock: a new environment beside the old one.
  writeFileSync(join(root, project, "uv.lock"), "lock-v2\n");
  assert.notEqual(sharedEnvironmentPath(root, project, deps), environment);
  assert.equal(sharedEnvironmentReady(root, project, deps), false);
  // uv failing, or the project itself installed into it (editable, bound to one checkout): refused.
  assert.throws(() => ensureSharedEnvironment(root, project, { depsRoot: deps, sync: () => ({ status: 1 }), log: () => {} }),
    /shared_python_env_unusable:uv sync failed/);
  const bumped = sharedEnvironmentPath(root, project, deps);
  const editable = (command, args, options) => {
    sync(command, args, options);
    writeFileSync(join(bumped, "lib", "python3.12", "site-packages", "_ai_trader_runtime.pth"), "/elsewhere/src\n");
    return { status: 0 };
  };
  assert.throws(() => ensureSharedEnvironment(root, project, { depsRoot: deps, sync: editable, log: () => {} }),
    /shared_python_env_unusable:.*itself installed/);
  assert.equal(existsSync(join(bumped, LOCK_MARKER)), false);
});

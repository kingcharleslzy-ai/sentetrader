import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { createHash } from "node:crypto";
import { PUBLIC_REPO, changelogSection, publicAssets, publicAttribution, publicChecksums, publicReleaseNotes, rewriteSiteSnapshot, syncPublicRelease } from "./sync_public_repo.mjs";

test("page snapshot points root-relative URLs at the official site and leaves the rest alone", () => {
  const html = [
    '<a href="/downloads/ai-trader/AI-Trader_latest_aarch64.dmg">',
    "<div data-poster=\"/static/a.webp\" style=\"--p:url('/static/a.webp')\">",
    '@font-face{src:url("/static/fonts/x.woff2")}',
    'fetch("/ai-trader-updates/latest.json")',
    '<a href="#log"><a href="https://media.qilinshuzhi.com/x.mp4"><a href="//cdn.example/x">',
    '<script>if (location.pathname === "/trader/download") init(); let p="/x";</script>',
    '<img srcset="/s.png 1x, /l.png 2x, https://cdn.example/x.png 3x">',
  ].join("\n");
  const out = rewriteSiteSnapshot(html, "https://qilinshuzhi.com");
  assert.match(out, /href="https:\/\/qilinshuzhi\.com\/downloads\/ai-trader\/AI-Trader_latest_aarch64\.dmg"/);
  assert.match(out, /data-poster="https:\/\/qilinshuzhi\.com\/static\/a\.webp"/);
  assert.match(out, /url\('https:\/\/qilinshuzhi\.com\/static\/a\.webp'\)/);
  assert.match(out, /url\("https:\/\/qilinshuzhi\.com\/static\/fonts\/x\.woff2"\)/);
  assert.match(out, /fetch\("https:\/\/qilinshuzhi\.com\/ai-trader-updates\/latest\.json"\)/);
  assert.match(out, /href="#log"/);
  assert.match(out, /href="https:\/\/media\.qilinshuzhi\.com\/x\.mp4"/);
  assert.match(out, /href="\/\/cdn\.example\/x"/);
  // Script comparisons and assignments keep their paths: only URL-bearing attributes are rewritten.
  assert.match(out, /location\.pathname === "\/trader\/download"/);
  assert.match(out, /let p="\/x"/);
  assert.match(out, /srcset="https:\/\/qilinshuzhi\.com\/s\.png 1x, https:\/\/qilinshuzhi\.com\/l\.png 2x, https:\/\/cdn\.example\/x\.png 3x"/);
  assert.ok(out.startsWith("<!-- 官网"));
});

const CHANGELOG = [
  "## Unreleased",
  "",
  "## 1.8.0 - 2026-10-17",
  "",
  "- 某项改动",
  "  - 维护记录：Claude Code / Opus 5.5，2026-10-18；main CI 123 全绿；备份 /opt/ai-trader/backups/x",
  "",
  "### 维护记录",
  "",
  "执行：Claude Code 执行代理 / Claude Opus 5.5（#525 修复、版本准备），主代理 Claude Opus 5.5 派工与验收，2026-10-17；发布记录：见 GitHub Release",
  "",
  "## 1.7.2 - 2026-10-06",
  "",
].join("\n");
const ATTRIBUTION = "执行：Claude Code 执行代理 / Claude Opus 5.5，主代理 Claude Opus 5.5 派工与验收，2026-10-17";

test("public notes carry the agent, model and date from the version's 维护记录, without internal details", () => {
  assert.equal(publicAttribution(changelogSection(CHANGELOG, "1.8.0")), ATTRIBUTION);
  assert.equal(publicAttribution("### 维护记录\n\n执行：Codex / GPT-6，2026-10-06。发布记录：见 Release\n"), "执行：Codex / GPT-6，2026-10-06");
  assert.throws(() => publicAttribution(changelogSection(CHANGELOG, "1.7.2")), /sync_public_repo_attribution_missing/);
  assert.throws(() => changelogSection(CHANGELOG, "9.9.9"), /sync_public_repo_changelog_section_missing/);
});

test("public release carries only the installers and checksums, with generic notes", () => {
  assert.deepEqual(publicAssets("1.8.0"), ["AI-Trader_1.8.0_aarch64.dmg", "AI-Trader_1.8.0_x64-setup.exe", "SHA256SUMS"]);
  const notes = publicReleaseNotes("1.8.0", ATTRIBUTION);
  assert.match(notes, /维护记录：执行：Claude Code 执行代理 \/ Claude Opus 5\.5，主代理 Claude Opus 5\.5 派工与验收，2026-10-17/);
  assert.match(notes, /v1\.8\.0/);
  assert.match(notes, /邀请码：sente/);
  assert.doesNotMatch(notes, /Co-Authored|ssh|\.tar\.gz|\/opt\/|CI \d|#525/i);
});

const sha = (text) => createHash("sha256").update(text).digest("hex");
const SUMS = (version, dmg = "dmg-bytes", exe = "exe-bytes") => [
  `${"a".repeat(64)}  AI-Trader_${version}_aarch64.app.tar.gz`,
  `${sha(dmg)}  AI-Trader_${version}_aarch64.dmg`,
  `${sha(exe)}  AI-Trader_${version}_x64-setup.exe`,
  `${"d".repeat(64)}  AI-Trader_1.7.2-${version}_aarch64.delta`,
  "",
].join("\n");

test("public SHA256SUMS lists only the two uploaded installers", () => {
  assert.equal(publicChecksums(SUMS("1.8.0"), "1.8.0"), `${sha("dmg-bytes")}  AI-Trader_1.8.0_aarch64.dmg\n${sha("exe-bytes")}  AI-Trader_1.8.0_x64-setup.exe\n`);
  assert.throws(() => publicChecksums(`${"b".repeat(64)}  AI-Trader_1.8.0_aarch64.dmg\n`, "1.8.0"), /sync_public_repo_checksums_incomplete/);
});

function withAssets(version, fn, { exe = "exe-bytes" } = {}) {
  const dir = mkdtempSync(join(tmpdir(), "public-repo-test-"));
  try {
    const [dmgName, exeName] = publicAssets(version);
    writeFileSync(join(dir, dmgName), "dmg-bytes");
    writeFileSync(join(dir, exeName), exe);
    writeFileSync(join(dir, "SHA256SUMS"), SUMS(version));
    return fn(dir);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test("a new version creates the public release with the three files", () => withAssets("1.8.0", (dir) => {
  const calls = [];
  const gh = (cmd, args) => {
    calls.push(args);
    if (args[1] === "view") return { code: 1, out: "", err: "release not found" };
    return { code: 0, out: `https://github.com/${PUBLIC_REPO}/releases/tag/v1.8.0`, err: "" };
  };
  const url = syncPublicRelease({ version: "1.8.0", assetsDir: dir, attribution: ATTRIBUTION, gh });
  assert.match(url, /sentetrader\/releases\/tag\/v1\.8\.0/);
  const create = calls.find((args) => args[1] === "create");
  assert.deepEqual(create.slice(0, 5), ["release", "create", "v1.8.0", "--repo", PUBLIC_REPO]);
  assert.equal(create.some((arg) => arg.startsWith("--latest")), false, "a backfilled older version must not become Latest");
  assert.deepEqual(create.slice(-3).map((p) => p.split("/").pop()), publicAssets("1.8.0"));
  assert.ok(create.at(-3).startsWith(dir) && create.at(-2).startsWith(dir), "installers come from the assets dir");
  assert.ok(!create.at(-1).startsWith(dir), "SHA256SUMS is the filtered public copy");
}));

test("a rerun only uploads the assets an earlier run missed", () => withAssets("1.8.0", (dir) => {
  const calls = [];
  const gh = (cmd, args) => {
    calls.push(args);
    if (args[1] === "view") return { code: 0, out: `https://github.com/${PUBLIC_REPO}/releases/tag/v1.8.0 AI-Trader_1.8.0_aarch64.dmg`, err: "" };
    return { code: 0, out: "", err: "" };
  };
  syncPublicRelease({ version: "1.8.0", assetsDir: dir, attribution: ATTRIBUTION, gh });
  assert.equal(calls.some((args) => args[1] === "create"), false);
  assert.ok(calls.some((args) => args[1] === "edit" && args.includes("--notes-file")), "an existing release gets its notes refreshed");
  const upload = calls.find((args) => args[1] === "upload");
  assert.deepEqual(upload.slice(5).map((p) => p.split("/").pop()), ["AI-Trader_1.8.0_x64-setup.exe", "SHA256SUMS"]);
}));

test("an installer that does not match SHA256SUMS is never published", () => withAssets("1.8.0", (dir) => {
  const gh = () => assert.fail("gh must not run");
  assert.throws(() => syncPublicRelease({ version: "1.8.0", assetsDir: dir, attribution: ATTRIBUTION, gh }), /sync_public_repo_digest_mismatch:AI-Trader_1\.8\.0_x64-setup\.exe/);
}, { exe: "tampered" }));

test("a missing installer stops the public release before anything is published", () => {
  const dir = mkdtempSync(join(tmpdir(), "public-repo-test-"));
  try {
    const gh = () => assert.fail("gh must not run");
    assert.throws(() => syncPublicRelease({ version: "1.8.0", assetsDir: dir, attribution: ATTRIBUTION, gh }), /sync_public_repo_asset_missing/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

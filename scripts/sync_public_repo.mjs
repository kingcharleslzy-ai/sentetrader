// Sync the public repository kingcharleslzy-ai/sentetrader (owner 2026-10-18): this script writes the static
// snapshot of the official Sente page (served by GitHub Pages from docs/) and the installers of every desktop
// release. The source code goes there separately through copy.bara.sky (AGPL-3.0, owner 2026-10-31).
//
//   node scripts/sync_public_repo.mjs --version 1.8.0 --assets-dir <dir> [--dry-run]   release + page snapshot
//   node scripts/sync_public_repo.mjs --site-only [--dry-run]                           page snapshot only
//
// release_all calls syncPublicRepo after the private GitHub Release with the same SHA-256-checked files;
// a failure is reported in the release summary and never fails the release. Re-running is safe: an
// existing release only gets missing assets uploaded (and its notes refreshed), an unchanged page is not
// committed. Installers are checked against SHA256SUMS here too, since this entry also runs on its own.
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const PUBLIC_REPO = "kingcharleslzy-ai/sentetrader";
export const SITE_ORIGIN = "https://qilinshuzhi.com";
export const SITE_PAGE = `${SITE_ORIGIN}/trader/download`;
export const SNAPSHOT_PATH = "docs/index.html";

/** The release files the public repository carries: the two installers and their checksums. */
export const publicAssets = (version) => [`AI-Trader_${version}_aarch64.dmg`, `AI-Trader_${version}_x64-setup.exe`, "SHA256SUMS"];

/** The release SHA256SUMS also lists the updater archive and deltas; the public copy keeps only the two installers. */
export function publicChecksums(sums, version) {
  const keep = publicAssets(version).slice(0, 2);
  const lines = sums.split("\n").filter((line) => keep.some((file) => line.trim().endsWith(`  ${file}`) || line.trim().endsWith(` *${file}`)));
  if (lines.length !== keep.length) throw new Error("sync_public_repo_checksums_incomplete");
  return lines.join("\n") + "\n";
}

/** The version's section of CHANGELOG.md (between its "## <version> - " heading and the next "## "). */
export function changelogSection(changelog, version) {
  const start = changelog.indexOf(`## ${version} - `);
  if (start < 0) throw new Error(`sync_public_repo_changelog_section_missing:${version}`);
  const bodyStart = changelog.indexOf("\n", start) + 1;
  const next = changelog.indexOf("\n## ", bodyStart);
  return changelog.slice(bodyStart, next < 0 ? undefined : next);
}

/**
 * Public-safe attribution (AGENTS.md: GitHub release notes carry the executing agent, model and date): the first
 * line of the section's "### 维护记录", without its parenthetical scope notes and cut before the first "；" or "。", so
 * hosts, CI ids and backup paths in the private record stay out.
 */
export function publicAttribution(section) {
  const at = section.indexOf("### 维护记录");
  const line = at < 0 ? "" : (section.slice(at).split("\n").slice(1).find((text) => text.trim()) || "");
  const text = line.trim().replace(/（[^）]*）/g, "").split(/[；。]/)[0].trim();
  if (!text) throw new Error("sync_public_repo_attribution_missing");
  return text.startsWith("执行") ? text : `执行：${text}`;
}

/** Public notes stay generic: the private CHANGELOG carries maintenance records and internal references. */
export const publicReleaseNotes = (version, attribution) => [
  `Sente（弈先）v${version}`,
  "",
  `- macOS 13+ Apple 芯片：AI-Trader_${version}_aarch64.dmg`,
  `- Windows 10/11 x64：AI-Trader_${version}_x64-setup.exe`,
  "- SHA256SUMS：安装包校验值",
  "",
  `更新内容见官网：${SITE_PAGE}#log`,
  "",
  "注册邀请码：sente。模拟交易，不构成投资建议。",
  "",
  `维护记录：${attribution}`,
  "",
].join("\n");

/** Make the official page work from github.io: root-relative URLs (attributes, CSS url(), fetch) point at the site. */
export function rewriteSiteSnapshot(html, origin = SITE_ORIGIN) {
  const stamp = `<!-- 官网 ${SITE_PAGE} 的静态快照，由 ai-trader scripts/sync_public_repo.mjs 生成，以官网为准 -->\n`;
  const absolute = (url) => (url.startsWith("/") && !url.startsWith("//") ? `${origin}${url}` : url);
  // Only URL-bearing attributes: a bare `="/…"` would also hit script comparisons like `pathname === "/x"`.
  // srcset carries several candidates ("/a.png 1x, /b.png 2x"); each one is rewritten.
  return stamp + html
    .replace(/(\ssrcset\s*=\s*)(["'])([^"']*)\2/g, (_, head, quote, value) =>
      `${head}${quote}${value.split(",").map((part) => part.replace(/^(\s*)(\S+)/, (__, space, url) => space + absolute(url))).join(",")}${quote}`)
    .replace(/(\s(?:href|src|poster|action|data-[\w-]+)\s*=\s*["'])\/(?!\/)/g, `$1${origin}/`)
    .replace(/(url\(\s*["']?)\/(?!\/)/g, `$1${origin}/`)
    .replace(/(fetch\(\s*["'`])\/(?!\/)/g, `$1${origin}/`);
}

const run = (cmd, args, { input } = {}) => {
  const result = spawnSync(cmd, args, { encoding: "utf8", input, maxBuffer: 64 * 1024 * 1024 });
  return { code: result.status ?? 1, out: (result.stdout || "").trim(), err: (result.stderr || "").trim() };
};
const must = (cmd, args, opts) => {
  const result = run(cmd, args, opts);
  if (result.code !== 0) throw new Error(`sync_public_repo_${cmd}_failed: ${args.slice(0, 3).join(" ")}: ${result.err || result.out}`);
  return result.out;
};

/** Create the public release (or upload what an earlier run missed). Returns the release URL. */
export function syncPublicRelease({ version, assetsDir, attribution, dryRun = false, gh = run }) {
  if (!attribution) throw new Error("sync_public_repo_attribution_missing");
  const source = publicAssets(version).map((file) => join(assetsDir, file));
  for (const file of source) if (!dryRun && !existsSync(file)) throw new Error(`sync_public_repo_asset_missing:${file}`);
  const tag = `v${version}`;
  if (dryRun) return `[dry-run] gh release create ${tag} --repo ${PUBLIC_REPO} + ${source.length} assets`;
  // This entry also runs on its own, so it checks the installers against SHA256SUMS itself before publishing.
  const checksums = publicChecksums(readFileSync(source[2], "utf8"), version);
  for (const file of source.slice(0, 2)) {
    const digest = createHash("sha256").update(readFileSync(file)).digest("hex");
    if (!checksums.includes(`${digest}  ${file.split("/").pop()}`) && !checksums.includes(`${digest} *${file.split("/").pop()}`)) {
      throw new Error(`sync_public_repo_digest_mismatch:${file.split("/").pop()}`);
    }
  }
  const dir = mkdtempSync(join(tmpdir(), "sente-public-"));
  try {
    const sums = join(dir, "SHA256SUMS");
    writeFileSync(sums, checksums);
    const notes = join(dir, "notes.md");
    writeFileSync(notes, publicReleaseNotes(version, attribution));
    return publishRelease({ tag, files: [source[0], source[1], sums], notes, gh });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

function publishRelease({ tag, files, notes, gh }) {
  const existing = gh("gh", ["release", "view", tag, "--repo", PUBLIC_REPO, "--json", "url,assets", "-q", "[.url, (.assets|map(.name)|join(\",\"))]|join(\" \")"]);
  if (existing.code === 0) {
    const [url, names = ""] = existing.out.split(" ");
    const missing = files.filter((file) => !names.split(",").includes(file.split("/").pop()));
    if (missing.length) {
      const up = gh("gh", ["release", "upload", tag, "--repo", PUBLIC_REPO, ...missing]);
      if (up.code !== 0) throw new Error(`sync_public_repo_upload_failed: ${up.err}`);
    }
    const edited = gh("gh", ["release", "edit", tag, "--repo", PUBLIC_REPO, "--notes-file", notes]);
    if (edited.code !== 0) throw new Error(`sync_public_repo_notes_failed: ${edited.err}`);
    return url;
  }
  // No --latest: GitHub picks Latest by date and version, so backfilling an older version does not take it over.
  const created = gh("gh", ["release", "create", tag, "--repo", PUBLIC_REPO, "--title", `Sente ${tag}`, "--notes-file", notes, ...files]);
  if (created.code !== 0) throw new Error(`sync_public_repo_release_failed: ${created.err}`);
  return created.out;
}

/** Refresh docs/index.html from the live page through the contents API (no clone). Returns "updated" or "unchanged". */
export function syncSiteSnapshot({ dryRun = false } = {}) {
  const page = must("curl", ["-fsS", "-m", "30", SITE_PAGE]);
  if (!page.includes("Sente")) throw new Error("sync_public_repo_page_unexpected");
  const html = rewriteSiteSnapshot(page);
  const content = Buffer.from(html).toString("base64");
  const current = run("gh", ["api", `repos/${PUBLIC_REPO}/contents/${SNAPSHOT_PATH}`, "-q", "[.sha, .content]|join(\" \")"]);
  const [sha = "", old = ""] = current.code === 0 ? current.out.split(" ") : [];
  if (old && Buffer.from(old.replace(/\\n|\s/g, ""), "base64").toString() === html) return "unchanged";
  if (dryRun) return `[dry-run] would update ${SNAPSHOT_PATH} (${html.length} bytes)`;
  const body = JSON.stringify({ message: "docs: 同步官网 Sente 页快照", content, ...(sha ? { sha } : {}) });
  must("gh", ["api", "-X", "PUT", `repos/${PUBLIC_REPO}/contents/${SNAPSHOT_PATH}`, "--input", "-"], { input: body });
  return "updated";
}

/** Both steps for release_all; never throws, the summary reports each step. */
export function syncPublicRepo({ version, assetsDir, changelog, dryRun = false }) {
  const result = {};
  try { result.release = syncPublicRelease({ version, assetsDir, attribution: publicAttribution(changelogSection(changelog, version)), dryRun }); } catch (error) { result.release = `failed: ${error.message}`; }
  try { result.site = syncSiteSnapshot({ dryRun }); } catch (error) { result.site = `failed: ${error.message}`; }
  return result;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  const value = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };
  const dryRun = args.includes("--dry-run");
  if (args.includes("--site-only")) {
    console.log(JSON.stringify({ site: syncSiteSnapshot({ dryRun }) }));
  } else {
    const version = value("--version");
    const assetsDir = value("--assets-dir");
    if (!version || !assetsDir) throw new Error("usage: --version <x.y.z> --assets-dir <dir> [--dry-run] | --site-only [--dry-run]");
    const changelog = readFileSync(resolve(fileURLToPath(new URL("..", import.meta.url)), "CHANGELOG.md"), "utf8");
    const result = syncPublicRepo({ version, assetsDir: resolve(assetsDir), changelog, dryRun });
    console.log(JSON.stringify(result));
    if (Object.values(result).some((v) => String(v).startsWith("failed"))) process.exit(1);
  }
}

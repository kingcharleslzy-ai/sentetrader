import { readFile, readdir, writeFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import { join, resolve } from "node:path";
import { constants, gzipSync } from "node:zlib";

// The web candidate has its own output directory: the desktop bundle embeds apps/client/dist, and
// a web build (base /trader/) landing there while Tauri compiles produced the blank 1.5.3.
const outDir = resolve(process.cwd(), "apps/client/dist-web");
const packageJson = JSON.parse(await readFile(resolve(process.cwd(), "package.json"), "utf8"));
const version = packageJson.version;
const commit = process.env.AI_TRADER_BUILD_SHA || "development";
// 网页版走官网同源 443 中转（qilinshuzhi-site 的 /trader-runtime/ 反代到腾讯云 8443，2026-10-11 #712 A）：
// 只放行 80/443 的网络也能用。桌面客户端仍直连 8443（runtimeConnection.ts 的 OWNER_RUNTIME_URL）。
const productionRuntimeUrl = "https://qilinshuzhi.com/trader-runtime";

if (process.env.VITE_CLOUD_RUNTIME_URL !== productionRuntimeUrl) {
  throw new Error("web_candidate_production_runtime_required");
}
if (process.env.VITE_DEFAULT_RUNTIME_MODE !== "cloud") {
  throw new Error("web_candidate_cloud_default_required");
}
if (
  process.env.AI_TRADER_BUILD_VERSION
  && process.env.AI_TRADER_BUILD_VERSION !== packageJson.version
) {
  throw new Error("web_candidate_source_version_mismatch");
}

// Under `npm run` npm exports its own cli path; running it through the current node binary
// avoids spawning `npm.cmd` on Windows, which Node >= 22 refuses without a shell (spawn EINVAL).
const npmCli = process.env.npm_execpath;
const [npmCommand, npmPrefix] = npmCli ? [process.execPath, [npmCli]] : ["npm", []];
await new Promise((resolvePromise, reject) => {
  const child = spawn(
    npmCommand,
    [...npmPrefix, "--workspace", "@ai-trader/client", "run", "build", "--", "--outDir", outDir, "--emptyOutDir"],
    { stdio: "inherit", env: process.env },
  );
  child.once("error", reject);
  child.once("exit", (code, signal) => {
    if (code === 0) resolvePromise();
    else reject(new Error(`web_candidate_build_failed:${code ?? signal ?? "unknown"}`));
  });
});
// version.json is written into the candidate only; the committed public/version.json stays untouched.
await writeFile(
  resolve(outDir, "version.json"),
  `${JSON.stringify({ version, commit, liveTrading: "disabled" }, null, 2)}\n`,
  "utf8",
);

// 更新日志 for the download page (qilinshuzhi.com/trader/download, owner 2026-09-30): the released
// sections of CHANGELOG.md (Unreleased skipped), one entry per top-level bullet without its
// 维护记录 attribution, truncated to its first sentence (at most 90 chars). Every web deploy
// refreshes it; the page lists only published versions.
const firstSentence = (text) => {
  const cut = text.indexOf("。");
  const sentence = cut === -1 ? text : text.slice(0, cut + 1);
  return sentence.length > 90 ? `${sentence.slice(0, 90)}…` : sentence;
};
const changelogVersions = [];
for (const section of (await readFile(resolve(process.cwd(), "CHANGELOG.md"), "utf8")).split(/^## /m).slice(1)) {
  const heading = section.match(/^(\d+\.\d+\.\d+)\s*-\s*(\d{4}-\d{2}-\d{2})/);
  if (!heading) continue;
  // A version's "### 要点" list (user-facing, written at release time) is the page's text; older
  // versions without one fall back to the first sentence of every entry.
  const lines = section.split("\n");
  const start = lines.findIndex((line) => line.trim() === "### 要点");
  const highlights = [];
  if (start >= 0) {
    for (const line of lines.slice(start + 1)) {
      if (line.startsWith("- ")) highlights.push(line.slice(2).replace(/\*\*|`/g, "").trim());
      else if (line.trim() && highlights.length) break;
    }
  }
  const items = (highlights.length ? highlights.filter((item) => !item.startsWith("（")) : lines.filter((line) => line.startsWith("- "))
    .map((line) => firstSentence(line.slice(2).replace(/\s*维护记录：.*$/, "").replace(/\*\*|`/g, "").trim())))
    .filter(Boolean);
  if (items.length) changelogVersions.push({ version: heading[1], date: heading[2], items });
}
// The newest 20 versions are plenty for the page (it shows 10); CHANGELOG lists newest first.
await writeFile(resolve(outDir, "changelog.json"), `${JSON.stringify({ versions: changelogVersions.slice(0, 20) })}\n`, "utf8");

// Precompressed copies for nginx gzip_static: the site host compresses on the fly at a low level
// (the 1.1 MB script went out as 407 KB) over ~4 Mbit/s, so the first visit waited 0.1-0.8 s for it.
// Level 9 once at build time is ~14% smaller; nginx falls back to the plain file without gzip.
const compressible = /\.(?:js|css|html|json|webmanifest|svg)$/;
for (const entry of await readdir(outDir, { recursive: true, withFileTypes: true })) {
  if (!entry.isFile() || !compressible.test(entry.name)) continue;
  const path = join(entry.parentPath ?? entry.path, entry.name);
  const body = await readFile(path);
  if (body.length < 1024) continue;
  await writeFile(`${path}.gz`, gzipSync(body, { level: constants.Z_BEST_COMPRESSION }));
}

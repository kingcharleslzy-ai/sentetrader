// Prepare a release version in one step: the 15-file version set the desktop preflight and the
// Runtime healthz assertion compare (the d59e1fd set), plus the CHANGELOG section skeleton with
// the 维护记录 line the repository rules require.
//
//   node scripts/prepare_release_version.mjs <version> [--date YYYY-MM-DD] [--scope "PR #… 范围一句话"]
//
// Nothing is committed; review the diff and open the version-prep PR as usual.
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const args = process.argv.slice(2);
const version = args[0] || "";
if (!/^\d+\.\d+\.\d+$/.test(version)) throw new Error("prepare_release_version_invalid");
const flag = (name) => { const index = args.indexOf(name); return index >= 0 ? args[index + 1] : undefined; };
const date = flag("--date") || new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10);
const scope = flag("--scope") || "（补充范围：自上一版本以来的 PR 与主题一句话）";

const current = JSON.parse(readFileSync(join(root, "package.json"), "utf8")).version;
if (current === version) throw new Error("prepare_release_version_unchanged");
const changed = [];
const rewrite = (path, transform) => {
  const before = readFileSync(join(root, path), "utf8");
  const after = transform(before);
  if (after === before) throw new Error(`prepare_release_version_no_change:${path}`);
  writeFileSync(join(root, path), after);
  changed.push(path);
};
const once = (pattern, replacement) => (text) => {
  if (!pattern.test(text)) throw new Error(`prepare_release_version_pattern_missing:${pattern}`);
  return text.replace(pattern, replacement);
};
const escaped = current.replace(/\./g, "\\.");

// JSON manifests: the first "version" key is the package's own.
for (const path of ["package.json", "apps/client/package.json", "apps/desktop/package.json", "packages/ui-events/package.json",
  "apps/client/public/version.json", "apps/desktop/src-tauri/tauri.conf.json"]) {
  rewrite(path, once(new RegExp(`"version": "${escaped}"`), `"version": "${version}"`));
}
// package-lock: the root twice and the three workspace packages, by name.
rewrite("package-lock.json", (text) => {
  let result = text.replace(new RegExp(`^(  "version": )"${escaped}",`, "m"), `$1"${version}",`);
  for (const name of ["ai-trader", "@ai-trader/client", "@ai-trader/desktop", "@ai-trader/ui-events"]) {
    const pattern = new RegExp(`("name": "${name.replace("/", "\\/")}",\\n\\s+"version": )"${escaped}"`);
    if (!pattern.test(result)) throw new Error(`prepare_release_version_lock_missing:${name}`);
    result = result.replace(pattern, `$1"${version}"`);
  }
  return result;
});
// Cargo and Python manifests.
rewrite("apps/desktop/src-tauri/Cargo.toml", once(new RegExp(`^version = "${escaped}"`, "m"), `version = "${version}"`));
rewrite("apps/desktop/src-tauri/Cargo.lock", once(new RegExp(`(name = "ai-trader-desktop"\\nversion = )"${escaped}"`), `$1"${version}"`));
for (const path of ["services/trader-runtime/pyproject.toml", "services/tqkq-sidecar/pyproject.toml"]) {
  rewrite(path, once(new RegExp(`^version = "${escaped}"`, "m"), `version = "${version}"`));
}
for (const path of ["services/trader-runtime/src/trader_runtime/__init__.py", "services/tqkq-sidecar/src/tqkq_readonly_sidecar/__init__.py"]) {
  rewrite(path, once(new RegExp(`__version__ = "${escaped}"`), `__version__ = "${version}"`));
}
for (const [path, name] of [["services/trader-runtime/uv.lock", "ai-trader-runtime"], ["services/tqkq-sidecar/uv.lock", "ai-trader-tqkq-sidecar"]]) {
  rewrite(path, once(new RegExp(`(name = "${name}"\\nversion = )"${escaped}"`), `$1"${version}"`));
}
if (changed.length !== 15) throw new Error(`prepare_release_version_file_count:${changed.length}`);

// Old-client compatibility baseline: regenerate from this exact source state. The version
// files above are already bumped, so the exported info.x-ai-trader.version is the release
// being prepared — the contract the desktop clients of this release will rely on. CI
// (tests/test_client_compat.py) then proves every later Runtime change against it; see
// services/trader-runtime/contracts/README.md. A failing export fails the prepare.
const head = execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim();
execFileSync(
  "uv", ["run", "--project", "services/trader-runtime", "python", "scripts/export_runtime_openapi.py",
    "--out", "services/trader-runtime/contracts/openapi-baseline.json", "--commit", head],
  { stdio: "inherit" });

// CHANGELOG: Unreleased entries move under the new section; the intro and 维护记录 are skeletons.
rewrite("CHANGELOG.md", (text) => {
  const head = "## Unreleased\n\n";
  const start = text.indexOf(head);
  if (start < 0) throw new Error("prepare_release_version_changelog_unreleased_missing");
  const bodyStart = start + head.length;
  const next = text.indexOf("\n## ", bodyStart);
  const entries = text.slice(bodyStart, next < 0 ? text.length : next).trim();
  // 要点: the download page shows this user-facing list (build:web skips the placeholder bullet);
  // 维护记录: fill in the agents that actually did the work — no hard-coded names.
  const section = `## ${version} - ${date}\n\n${scope}安装说明：macOS 已 Apple 签名并公证；Windows 安装包未签名，首次打开需在系统提示里允许。\n\n`
    + `### 要点\n\n- （填写：面向用户的 3–8 条要点，下载页展示）\n\n### 全部改动\n\n`
    + (entries ? `${entries}\n\n` : "")
    + `### 维护记录\n\n执行：（填写：执行 agent 与模型、各自负责的部分），${date}。发布记录：见 GitHub Release v${version} 说明；源码 main 发布 SHA 以 Release 与 \`healthz.commit\` 为准。\n\n`;
  return `${text.slice(0, bodyStart)}${section}${next < 0 ? "" : text.slice(next + 1)}`;
});

// `changed` holds the 15 version files + CHANGELOG; the +1 is the regenerated baseline.
console.log(JSON.stringify({ status: "prepared", from: current, to: version, files: changed.length + 1, date }, null, 2));
console.log("Next: review `git diff --stat` (15 version files + CHANGELOG + contracts/openapi-baseline.json), fill the CHANGELOG intro, then `npm run release:desktop:preflight -- --configuration-only`.");

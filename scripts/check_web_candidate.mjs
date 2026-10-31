import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import { resolve } from "node:path";

function normalizeBasePath(raw) {
  if (!raw || raw === "/") return "/";
  const path = raw.startsWith("/") ? raw : `/${raw}`;
  return path.endsWith("/") ? path : `${path}/`;
}

const root = resolve(process.cwd(), "apps/client/dist-web");
const base = normalizeBasePath(process.env.VITE_APP_BASE_PATH);
const [index, manifestText, versionText, nginxConfig, productionEnvironment, packageText] = await Promise.all([
  readFile(resolve(root, "index.html"), "utf8"),
  readFile(resolve(root, "manifest.webmanifest"), "utf8"),
  readFile(resolve(root, "version.json"), "utf8"),
  readFile(resolve(process.cwd(), "deploy/web/nginx-ai-trader-path.conf"), "utf8"),
  readFile(resolve(process.cwd(), "deploy/web/.env.production.example"), "utf8"),
  readFile(resolve(process.cwd(), "package.json"), "utf8"),
]);
const manifest = JSON.parse(manifestText);
// The network-only service worker (2026-10-06): served from the site next to the page, never a
// precaching worker — a precache list would bring back old shells after a release.
const serviceWorker = await readFile(resolve(root, "sw.js"), "utf8");
assert.ok(serviceWorker.includes("respondWith") && serviceWorker.includes("navigate"), "web_service_worker_fetch_handler_missing");
assert.ok(!/__WB_MANIFEST|precacheAndRoute|workbox-precaching|\brevision:/.test(serviceWorker), "web_service_worker_precaches");
assert.equal(manifest.id, base, "web_manifest_id_mismatch");
await readFile(resolve(root, "icons/apple-touch-icon.png"));
assert.match(index, new RegExp(`<link rel="apple-touch-icon" href="${base.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}icons/apple-touch-icon\\.png"`), "web_apple_touch_icon_missing");
const version = JSON.parse(versionText);
const sourcePackage = JSON.parse(packageText);
const assetNames = await readdir(resolve(root, "assets"));
const javascript = (
  await Promise.all(
    assetNames
      .filter((name) => name.endsWith(".js"))
      .map((name) => readFile(resolve(root, "assets", name), "utf8")),
  )
).join("\n");

assert.equal(manifest.start_url, base, "web_manifest_start_url_mismatch");
assert.equal(manifest.scope, base, "web_manifest_scope_mismatch");
assert.ok(
  manifest.icons.every((icon) => typeof icon.src === "string" && icon.src.startsWith(`${base}icons/`)),
  "web_manifest_icon_base_mismatch",
);
assert.match(index, new RegExp(`(?:href|src)=["']${base.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`));
assert.equal(version.liveTrading, "disabled", "web_live_trading_boundary_failed");
assert.equal(version.version, sourcePackage.version, "web_source_version_mismatch");
if (process.env.AI_TRADER_BUILD_SHA) {
  assert.equal(version.commit, process.env.AI_TRADER_BUILD_SHA, "web_source_commit_mismatch");
}
assert.ok(!index.includes("http://127.0.0.1"), "web_candidate_contains_localhost_runtime");
const assetOrigin = process.env.VITE_ASSET_ORIGIN;
assert.ok(assetOrigin && productionEnvironment.includes(`VITE_ASSET_ORIGIN=${assetOrigin}`), "web_asset_origin_missing");
assert.match(index, new RegExp(`<script type="module" crossorigin src="${assetOrigin.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}assets/`), "web_script_not_on_asset_origin");
// 'wasm-unsafe-eval' and worker-src 'self' (2026-09-30): the strategy lab runs Pyodide in a same-origin worker.
// Three CSP blocks carry it: version.json, /trader/assets/ and /trader/ (which also serves /trader/admin).
assert.equal(
  nginxConfig.match(new RegExp(`script-src 'self' 'wasm-unsafe-eval' ${new URL(assetOrigin).origin}; worker-src 'self';`, "g"))?.length, 3,
  "web_asset_origin_missing_from_content_security_policy",
);
// 后台管理 is /trader/admin (2026-10-01): the /trader/ fallback serves it; no separate location.
assert.match(nginxConfig, /try_files \$uri \$uri\/ \/trader\/index\.html;/, "web_trader_fallback_missing");
// 网页版经官网同源 /trader-runtime/ 中转访问 Runtime（#712 A）；CSP 的 connect-src 仍保留 8443 直连地址。
const runtimeUrl = "https://qilinshuzhi.com/trader-runtime";
const directRuntimeOrigin = "https://ai-runtime.qilinshuzhi.com:8443";
assert.ok(
  javascript.includes(runtimeUrl),
  "web_bundle_runtime_origin_mismatch",
);
assert.ok(
  productionEnvironment.includes(`VITE_CLOUD_RUNTIME_URL=${runtimeUrl}`),
  "web_production_runtime_origin_mismatch",
);
assert.match(nginxConfig, /location \^~ \/trader-runtime\/ \{[^}]*proxy_pass https:\/\/sente_runtime_relay\/;/, "web_runtime_relay_missing");
assert.equal(
  // wss://qilinshuzhi.com：老浏览器的 'self' 不匹配 wss，语音三条 WebSocket 经中转要显式放行。
  nginxConfig.match(new RegExp(`connect-src 'self' wss://qilinshuzhi.com ${directRuntimeOrigin}`, "g"))?.length,
  3,
  "web_runtime_origin_missing_from_content_security_policy",
);

console.log(JSON.stringify({
  status: "passed",
  target: "web-candidate",
  basePath: base,
  liveTrading: version.liveTrading,
}));

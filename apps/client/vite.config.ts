import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";
import clientPackage from "./package.json";

function normalizedBasePath(raw: string | undefined): string {
  if (!raw || raw === "/") return "/";
  const path = raw.startsWith("/") ? raw : `/${raw}`;
  return path.endsWith("/") ? path : `${path}/`;
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const base = normalizedBasePath(env.VITE_APP_BASE_PATH);
  // Who this build is: every Runtime request reports it (X-AI-Trader-Client).
  // `AI_TRADER_BUILD_SHA` is set by the web/desktop build pipelines, absent in local dev.
  const buildSha = (globalThis as { process?: { env?: Record<string, string | undefined> } })
    .process?.env?.AI_TRADER_BUILD_SHA || "dev";
  // Web build only: the hashed scripts and styles load from Aliyun OSS (steady ~11 MB/s from
  // Shanghai) instead of the site host (~4 Mbit/s, 0.1-0.8 s for the main script). The page,
  // manifest and icons stay on the site; deploy-web.sh uploads assets/ there before switching.
  const assetOrigin = env.VITE_ASSET_ORIGIN;
  return {
    base,
    experimental: assetOrigin ? {
      renderBuiltUrl: (filename: string) => filename.startsWith("assets/") ? `${assetOrigin}${filename}` : undefined,
    } : undefined,
    define: {
      __AI_TRADER_CLIENT_VERSION__: JSON.stringify(clientPackage.version),
      __AI_TRADER_BUILD_SHA__: JSON.stringify(buildSha)
    },
    resolve: { alias: { "@ai-trader/ui-events": decodeURIComponent(new URL("../../packages/ui-events/src/index.ts", import.meta.url).pathname).replace(/^\/([A-Za-z]:\/)/, "$1") } },
    // The browser backtest engine (strategyEngine/engineWorker.ts) is an ES-module worker (it dynamic-imports Pyodide's
    // pyodide.mjs), and Vite's worker default of "iife" cannot build such an import at all.
    worker: { format: "es" },
    plugins: [
      react(),
      {
        // The web page opens its Runtime connection (DNS + TLS, ~50 ms each from China) while
        // the script is still downloading, instead of after the app has started.
        name: "ai-trader-runtime-preconnect",
        transformIndexHtml: () => env.VITE_CLOUD_RUNTIME_URL
          ? [{ tag: "link", attrs: { rel: "preconnect", href: new URL(env.VITE_CLOUD_RUNTIME_URL).origin, crossorigin: "use-credentials" }, injectTo: "head" }]
          : [],
      },
      // The manifest, and src/sw.js built to sw.js as is: a network-only worker with no precache
      // list (injectionPoint undefined), registered by main.tsx itself (injectRegister false).
      VitePWA({
        strategies: "injectManifest",
        srcDir: "src",
        filename: "sw.js",
        injectRegister: false,
        injectManifest: { injectionPoint: undefined, rollupFormat: "iife" },
        includeAssets: ["icons/favicon.png", "icons/icon-192.png", "icons/icon-512.png", "icons/apple-touch-icon.png"],
        manifest: {
          id: base,
          name: "Sente",
          short_name: "Sente",
          description: "Sente · AI 私人交易助理",
          start_url: base,
          scope: base,
          // Installed on a desktop (Chrome/Edge), the title bar folds into the page header and the
          // window buttons sit in its left edge, as in the desktop client; standalone is the fallback.
          display_override: ["window-controls-overlay"],
          display: "standalone",
          // The dark theme (the default) app background and header surface; theme.ts keeps the
          // page's theme-color meta on the current theme's header colour.
          background_color: "#060708",
          theme_color: "#0b0d10",
          lang: "zh-CN",
          icons: [
            {
              src: `${base}icons/icon-192.png`,
              sizes: "192x192",
              type: "image/png",
              purpose: "any maskable"
            },
            {
              src: `${base}icons/icon-512.png`,
              sizes: "512x512",
              type: "image/png",
              purpose: "any maskable"
            }
          ]
        }
      }),
    ],
    server: {
      host: "127.0.0.1",
      port: 5173,
      // 回测本机优先 (2026-10-12): the dev page loads Pyodide same-origin like the site does
      // (strategyEngine/browserEngine.ts pyodideUrl); the files come from the site's self-hosted copy.
      proxy: { "/trader/pyodide/": { target: "https://qilinshuzhi.com", changeOrigin: true, secure: true } }
    }
  };
});

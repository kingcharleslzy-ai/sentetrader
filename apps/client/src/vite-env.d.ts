/// <reference types="vite/client" />

/** Build-time constants injected by vite.config.ts `define` (version from package.json, sha from the build pipeline, "dev" locally). */
declare const __AI_TRADER_CLIENT_VERSION__: string;
declare const __AI_TRADER_BUILD_SHA__: string;

interface Window {
  __TAURI_INTERNALS__?: unknown;
  __AI_TRADER_CLOUD_ACCESS_TOKEN__?: string;
}
/// <reference types="vite-plugin-pwa/client" />

interface ImportMetaEnv {
  readonly VITE_LOCAL_RUNTIME_URL?: string;
  readonly VITE_CLOUD_RUNTIME_URL?: string;
  readonly VITE_LOCAL_RUNTIME_TOKEN?: string;
  readonly VITE_CLOUD_RUNTIME_TOKEN?: string;
  readonly VITE_CLOUD_AUTH_MODE?: "local-token" | "cloud-jwt" | "owner-password";
  readonly VITE_DEFAULT_RUNTIME_MODE?: "local" | "cloud";
  readonly VITE_TEST_BUILD?: string;
  readonly VITE_ALLOW_INSECURE_LOCAL_CLOUD?: "0" | "1";
  readonly VITE_APP_BASE_PATH?: string;
  /** Where the browser backtest engine downloads Pyodide from; blank means the built-in choice (the site's
   * self-hosted copy: same origin on the web and through the dev server proxy, qilinshuzhi.com on desktop). */
  readonly VITE_PYODIDE_URL?: string;
  readonly VITE_ASSET_ORIGIN?: string;
}

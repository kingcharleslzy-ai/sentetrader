import type { RuntimeMode } from "@ai-trader/ui-events";

export interface RuntimeConnectionConfig {
  baseUrl: string;
  token: string;
  authMode: "local-token" | "cloud-jwt" | "owner-password";
  source: "web-env" | "tauri-ipc";
  liveTrading: false;
}

export const OWNER_RUNTIME_URL = "https://ai-runtime.qilinshuzhi.com:8443";

const normalizeUrl = (url: string) => url.replace(/\/$/, "");

export function validateCloudRuntimeUrl(
  raw: string | undefined,
  allowInsecureLocalhost = false
): string | undefined {
  if (!raw) return undefined;
  try {
    const url = new URL(raw);
    const local = url.hostname === "127.0.0.1" || url.hostname === "localhost";
    if (url.protocol !== "https:" && !(allowInsecureLocalhost && local && url.protocol === "http:")) {
      return undefined;
    }
    if (url.username || url.password || url.search || url.hash) return undefined;
    return normalizeUrl(url.toString());
  } catch {
    return undefined;
  }
}

export function configuredRuntimeUrl(mode: RuntimeMode): string | undefined {
  if (mode === "local") {
    return normalizeUrl(import.meta.env.VITE_LOCAL_RUNTIME_URL || "http://127.0.0.1:8000");
  }
  // 自建版 (deploy/self-host): the Runtime serves this page, so the page's own origin is the Runtime,
  // whatever address the user opened it on; http there is the page's own scheme, not a downgrade.
  if (import.meta.env.VITE_CLOUD_RUNTIME_URL === "same-origin" && typeof window !== "undefined") {
    return normalizeUrl(window.location.origin);
  }
  return validateCloudRuntimeUrl(
    import.meta.env.VITE_CLOUD_RUNTIME_URL || (isTauriRuntime() ? OWNER_RUNTIME_URL : undefined),
    import.meta.env.VITE_ALLOW_INSECURE_LOCAL_CLOUD === "1"
  );
}

export function isCloudRuntimeConfigured(): boolean {
  return Boolean(configuredRuntimeUrl("cloud"));
}

/** The desktop is cloud-only (owner decision 2026-09-15); the web build keeps "local" for the
 *  token-authenticated development / e2e runtime. */
export function defaultRuntimeMode(): RuntimeMode {
  if (isTauriRuntime()) return "cloud";
  return import.meta.env.VITE_DEFAULT_RUNTIME_MODE === "cloud" && isCloudRuntimeConfigured()
    ? "cloud"
    : "local";
}

export function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

function webRuntimeUrl(mode: RuntimeMode): string {
  const configured = configuredRuntimeUrl(mode);
  if (!configured) throw new Error("云端服务尚未配置安全连接地址。");
  return configured;
}

function webRuntimeToken(mode: RuntimeMode): string {
  if (mode === "cloud") {
    const memoryToken = typeof window !== "undefined" ? window.__AI_TRADER_CLOUD_ACCESS_TOKEN__ : undefined;
    if (memoryToken) return memoryToken;
    return import.meta.env.DEV ? import.meta.env.VITE_CLOUD_RUNTIME_TOKEN || "" : "";
  }
  return mode === "local"
    ? import.meta.env.VITE_LOCAL_RUNTIME_TOKEN || ""
    : "";
}

export function cloudRuntimeAuthMode(): "local-token" | "cloud-jwt" | "owner-password" {
  if (import.meta.env.VITE_CLOUD_AUTH_MODE === "local-token") return "local-token";
  if (import.meta.env.VITE_CLOUD_AUTH_MODE === "cloud-jwt") return "cloud-jwt";
  return "owner-password";
}

function webAuthMode(mode: RuntimeMode): "local-token" | "cloud-jwt" | "owner-password" {
  if (mode === "local") return "local-token";
  return cloudRuntimeAuthMode();
}

export async function resolveRuntimeConnection(mode: RuntimeMode): Promise<RuntimeConnectionConfig> {
  if (mode === "cloud" && isTauriRuntime()) {
    const { invoke } = await import("@tauri-apps/api/core");
    const saved = await invoke<{ url: string; token: string } | null>("load_cloud_connection");
    return { baseUrl: OWNER_RUNTIME_URL, token: saved?.token ?? "", authMode: "owner-password", source: "tauri-ipc", liveTrading: false };
  }
  return {
    baseUrl: webRuntimeUrl(mode),
    token: webRuntimeToken(mode),
    authMode: webAuthMode(mode),
    source: "web-env",
    liveTrading: false,
  };
}

export function setCloudAccessToken(token?: string): void {
  if (typeof window === "undefined") return;
  if (token) window.__AI_TRADER_CLOUD_ACCESS_TOKEN__ = token;
  else delete window.__AI_TRADER_CLOUD_ACCESS_TOKEN__;
}

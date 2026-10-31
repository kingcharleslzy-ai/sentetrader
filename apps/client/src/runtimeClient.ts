/**
 * One client for every read of the Runtime: the TanStack QueryClient that
 * caches reads, the login-identity generation that retires them, and the
 * typed openapi-fetch client generated from the Runtime's OpenAPI schema.
 * Transactions keep the explicit request path in api.ts.
 *
 * api.ts imports resetReadIdentity from here and this module imports
 * runtimeFetch from api.ts: a deliberate cycle that only works because both
 * sides use the other's exports inside functions. Never reference an api.ts
 * export at this module's top level.
 */
import { useSyncExternalStore } from "react";
import { QueryClient } from "@tanstack/react-query";
import createClient from "openapi-fetch";
import { runtimeFetch } from "./api";
import type { paths, components } from "./workbench.generated";

export type WorkbenchConfig = components["schemas"]["WorkbenchConfig"];
export type ChartExtension = components["schemas"]["ChartExtension"];
export type ActivitySnapshot = components["schemas"]["ActivitySnapshot"];
export type Brief = components["schemas"]["Brief"];

// Only read queries use this cache. Transactions keep the existing request path.
export const readQueryClient = new QueryClient({ defaultOptions: {
  queries: { retry: false, refetchOnWindowFocus: false, gcTime: 0 },
  mutations: { retry: false, networkMode: "always" }
} });

export function readNetworkMode(baseUrl: string | undefined): "always" | "online" {
  if (baseUrl !== undefined) {
    const host = new URL(baseUrl || window.location.href, window.location.href).hostname;
    if (host === "localhost" || host === "127.0.0.1" || host === "[::1]") return "always";
  }
  return "online";
}

let identityGeneration = 0;
export const readIdentityGeneration = () => identityGeneration;
const listeners = new Set<() => void>();
/** A login, logout or account switch: every cached read and in-flight fence is retired. */
export function resetReadIdentity() {
  identityGeneration += 1;
  readQueryClient.clear();
  listeners.forEach(listener => listener());
}
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
export function useReadIdentity() {
  return useSyncExternalStore(subscribe, () => identityGeneration);
}

// The person signed in (#523): only a sign-in or sign-out moves it — not a reconnect, a status
// re-check or an account switch, which keep the same person. What belongs to that person and lives
// past a read (the last voice call's timeline for 诊断导出) is stamped with it and retired on a change.
let loginGeneration = 0;
export const loginIdentity = () => loginGeneration;
const loginListeners = new Set<() => void>();
/** A sign-in or sign-out: the previous person's local data is retired. */
export function changeLogin() {
  loginGeneration += 1;
  loginListeners.forEach(listener => listener());
}
export const onLoginChange = (listener: () => void) => { loginListeners.add(listener); return () => { loginListeners.delete(listener); }; };

export function workbenchClient(baseUrl: string) {
  return createClient<paths>({
    baseUrl: new URL(baseUrl || "/", window.location.href).href.replace(/\/$/, ""),
    fetch: runtimeFetch
  });
}

export function responseData<T>({ data, error, response }: { data?: T; error?: unknown; response: Response }): T {
  if (!response.ok || data === undefined) {
    throw Object.assign(new Error(`${response.status} ${JSON.stringify(error)}`), { status: response.status, detail: error });
  }
  return data;
}

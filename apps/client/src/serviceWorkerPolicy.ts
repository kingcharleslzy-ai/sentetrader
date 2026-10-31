type BrowserCacheEnvironment = {
  __TAURI_INTERNALS__?: unknown;
  location?: { href?: string };
  navigator?: {
    serviceWorker?: {
      getRegistrations?: () => Promise<ReadonlyArray<{
        scope?: string;
        unregister: () => Promise<boolean>;
      }>>;
    };
  };
  caches?: {
    keys: () => Promise<string[]>;
    delete: (name: string) => Promise<boolean>;
  };
};

/** Service workers are an optional enhancement (install offer, offline page, lock-screen push).
 *  A browser profile whose worker storage is damaged throws InvalidStateError / SecurityError from
 *  any of these calls; that must cost the feature, never the page (10-13: a blank page). All
 *  service-worker access in the app goes through the helpers below. */
function warn(action: string, error: unknown) {
  console.warn(`[service-worker] ${action} unavailable, continuing without it`, error);
}

function workerContainer(): ServiceWorkerContainer | undefined {
  try { return typeof navigator === "undefined" ? undefined : navigator.serviceWorker; } catch (error) {
    warn("access", error);
    return undefined;
  }
}

/** The page's registration, or undefined when there is none or the browser refuses to say. Never rejects. */
export async function currentServiceWorkerRegistration(): Promise<ServiceWorkerRegistration | undefined> {
  try { return await workerContainer()?.getRegistration?.(); } catch (error) {
    warn("getRegistration", error);
    return undefined;
  }
}

/** The active registration, waiting at most `timeoutMs` (`serviceWorker.ready` may never settle).
 *  Rejects with `worker_not_ready` on a timeout or any browser error. */
export async function readyServiceWorkerRegistration(timeoutMs = 8000): Promise<ServiceWorkerRegistration> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error("worker_not_ready")), timeoutMs); });
  try {
    const ready = workerContainer()?.ready;
    if (!ready) throw new Error("worker_not_ready");
    return await Promise.race([ready, timeout]);
  } catch (error) {
    if ((error as Error)?.message !== "worker_not_ready") warn("ready", error);
    throw new Error("worker_not_ready", { cause: error });
  } finally {
    clearTimeout(timer);
  }
}

/** Listen to the worker's messages; a no-op (returning a no-op stop) when the browser has no usable worker. */
export function listenToServiceWorker(handle: (event: MessageEvent) => void): () => void {
  const worker = workerContainer();
  if (!worker?.addEventListener) return () => undefined;
  try {
    worker.addEventListener("message", handle);
  } catch (error) {
    warn("message listener", error);
    return () => undefined;
  }
  return () => { try { worker.removeEventListener("message", handle); } catch { /* nothing to undo */ } };
}

/** The web app's registration call; undefined in dev or where the browser has no service worker. */
export function serviceWorkerRegistrar(url: string, scope: string): (() => Promise<unknown>) | undefined {
  const worker = import.meta.env.DEV ? undefined : workerContainer();
  return worker?.register ? () => worker.register(url, { scope }) : undefined;
}

function applicationPrefix(href?: string): string | undefined {
  if (!href) return undefined;
  const page = new URL(href);
  const marker = "/trader/";
  const index = page.pathname.indexOf(marker);
  if (index >= 0) {
    return new URL(page.pathname.slice(0, index + marker.length), page.origin).href;
  }
  return new URL("/", page.origin).href;
}

/** The old precaching worker's caches (workbox names carry the scope) and anything named ai-trader. */
function ownsCache(name: string, prefix?: string): boolean {
  const normalized = name.toLowerCase();
  return normalized.includes("ai-trader") || Boolean(prefix && name.includes(prefix));
}

/**
 * Service worker policy (2026-10-06). The desktop app never has one: every registration and cache
 * is removed. The web app registers `sw.js`, which only passes navigations to the network (an
 * offline page when that fails) and caches nothing — so a release can never be shadowed by an old
 * shell, the reason the precaching worker was retired. The same URL and scope replace a leftover
 * precaching worker in place (the new one takes control at once and deletes its caches); a
 * registration at another scope inside the app (e.g. /trader/assets/) is removed, and the old
 * caches are cleared here too.
 */
export async function configureServiceWorker(
  register: (() => Promise<unknown>) | undefined,
  environment: BrowserCacheEnvironment = window,
): Promise<"desktop-cache-cleared" | "web-registered" | "web-not-registered"> {
  try {
    return await applyServiceWorkerPolicy(register, environment);
  } catch (error) {
    warn("setup", error);
    return "__TAURI_INTERNALS__" in environment ? "desktop-cache-cleared" : "web-not-registered";
  }
}

async function applyServiceWorkerPolicy(
  register: (() => Promise<unknown>) | undefined,
  environment: BrowserCacheEnvironment,
): Promise<"desktop-cache-cleared" | "web-registered" | "web-not-registered"> {
  const workerApi = environment.navigator?.serviceWorker;
  const registrations = await workerApi?.getRegistrations?.() ?? [];
  const desktop = "__TAURI_INTERNALS__" in environment;
  const prefix = applicationPrefix(environment.location?.href);
  const removed = desktop || !prefix
    ? registrations
    : registrations.filter((registration) => (
      typeof registration.scope === "string" && registration.scope.startsWith(prefix) && registration.scope !== prefix
    ));
  await Promise.allSettled(removed.map((registration) => registration.unregister()));

  const cacheNames = await environment.caches?.keys() ?? [];
  const ownedCaches = desktop
    ? cacheNames
    : cacheNames.filter((name) => ownsCache(name, prefix));
  await Promise.allSettled(ownedCaches.map((name) => environment.caches!.delete(name)));

  if (desktop) return "desktop-cache-cleared";
  if (!register || !workerApi) return "web-not-registered";
  // Registration runs alongside the first render; a failure only means no install offer and no
  // offline page this time.
  void Promise.resolve().then(register).catch((error) => warn("register", error));
  return "web-registered";
}

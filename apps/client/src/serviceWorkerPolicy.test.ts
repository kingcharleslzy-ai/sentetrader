// @vitest-environment node
// Protects the service-worker invariant: the web app registers its network-only worker exactly once
// and clears the old precaching worker's state; the desktop app never runs any worker.
import { afterEach, expect, test, vi } from "vitest";
import { configureServiceWorker, currentServiceWorkerRegistration, listenToServiceWorker, readyServiceWorkerRegistration } from "./serviceWorkerPolicy";

const flush = () => new Promise(resolve => setTimeout(resolve, 0));

test("the web app registers its worker once and clears old workers and caches inside its prefix", async () => {
  const register = vi.fn().mockResolvedValue({});
  const unregisterApp = vi.fn().mockResolvedValue(true);
  const unregisterWide = vi.fn().mockResolvedValue(true);
  const unregisterOther = vi.fn().mockResolvedValue(true);
  const deleteCache = vi.fn().mockResolvedValue(true);
  const environment = {
    location: { href: "https://qilinshuzhi.com/trader/" },
    navigator: {
      serviceWorker: {
        getRegistrations: vi.fn().mockResolvedValue([
          { scope: "https://qilinshuzhi.com/trader/", unregister: unregisterApp },
          { scope: "https://qilinshuzhi.com/trader/assets/", unregister: unregisterWide },
          { scope: "https://qilinshuzhi.com/other/", unregister: unregisterOther },
        ]),
      },
    },
    caches: {
      keys: vi.fn().mockResolvedValue([
        "workbox-precache-v2-https://qilinshuzhi.com/trader/",
        "ai-trader-legacy-shell",
        "other-site-cache",
      ]),
      delete: deleteCache,
    },
  };

  await expect(configureServiceWorker(register, environment)).resolves.toBe("web-registered");
  await flush();
  expect(register).toHaveBeenCalledOnce();
  // The app's own scope is replaced in place by the new worker (same URL); other scopes inside it go.
  expect(unregisterApp).not.toHaveBeenCalled();
  expect(unregisterWide).toHaveBeenCalledOnce();
  expect(unregisterOther).not.toHaveBeenCalled();
  expect(deleteCache.mock.calls.map(call => call[0])).toEqual([
    "workbox-precache-v2-https://qilinshuzhi.com/trader/",
    "ai-trader-legacy-shell",
  ]);
});

test("a failed registration does not stop the page", async () => {
  const register = vi.fn().mockRejectedValue(new Error("blocked"));
  const environment = {
    location: { href: "https://qilinshuzhi.com/trader/" },
    navigator: { serviceWorker: { getRegistrations: vi.fn().mockResolvedValue([]) } },
    caches: { keys: vi.fn().mockResolvedValue([]), delete: vi.fn() },
  };
  await expect(configureServiceWorker(register, environment)).resolves.toBe("web-registered");
  await flush();
  expect(register).toHaveBeenCalledOnce();
});

test("without a register function (dev) or worker support nothing is registered", async () => {
  const environment = { location: { href: "http://127.0.0.1:5173/" }, caches: { keys: vi.fn().mockResolvedValue([]), delete: vi.fn() } };
  await expect(configureServiceWorker(undefined, { ...environment, navigator: { serviceWorker: { getRegistrations: vi.fn().mockResolvedValue([]) } } }))
    .resolves.toBe("web-not-registered");
  const register = vi.fn();
  await expect(configureServiceWorker(register, { ...environment, navigator: {} })).resolves.toBe("web-not-registered");
  expect(register).not.toHaveBeenCalled();
});

test("the desktop app removes every worker and cache and never registers", async () => {
  const register = vi.fn();
  const unregister = vi.fn().mockResolvedValue(true);
  const deleteCache = vi.fn().mockResolvedValue(true);
  const environment = {
    __TAURI_INTERNALS__: {},
    navigator: {
      serviceWorker: {
        getRegistrations: vi.fn().mockResolvedValue([{ unregister }]),
      },
    },
    caches: {
      keys: vi.fn().mockResolvedValue(["legacy-ai-trader-cache", "legacy-other-cache"]),
      delete: deleteCache,
    },
  };

  await expect(configureServiceWorker(register, environment)).resolves.toBe("desktop-cache-cleared");
  await flush();
  expect(register).not.toHaveBeenCalled();
  expect(unregister).toHaveBeenCalledOnce();
  expect(deleteCache).toHaveBeenCalledTimes(2);
});

// Optional enhancement: a browser that throws from any worker call costs the feature, not the page.
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

test("configureServiceWorker swallows an InvalidStateError from getRegistrations and from caches", async () => {
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
  const broken = new DOMException("broken", "InvalidStateError");
  const register = vi.fn();
  await expect(configureServiceWorker(register, {
    location: { href: "https://qilinshuzhi.com/trader/" },
    navigator: { serviceWorker: { getRegistrations: () => Promise.reject(broken) } },
    caches: { keys: vi.fn().mockResolvedValue([]), delete: vi.fn() },
  })).resolves.toBe("web-not-registered");
  await expect(configureServiceWorker(register, {
    __TAURI_INTERNALS__: {},
    navigator: { serviceWorker: { getRegistrations: vi.fn().mockResolvedValue([]) } },
    caches: { keys: () => Promise.reject(broken), delete: vi.fn() },
  })).resolves.toBe("desktop-cache-cleared");
  expect(register).not.toHaveBeenCalled();
});

test("getRegistration errors read as no registration; a container that throws on access is no worker", async () => {
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
  vi.stubGlobal("navigator", { serviceWorker: { getRegistration: () => Promise.reject(new DOMException("x", "InvalidStateError")) } });
  await expect(currentServiceWorkerRegistration()).resolves.toBeUndefined();
  vi.stubGlobal("navigator", { get serviceWorker() { throw new DOMException("x", "SecurityError"); } });
  await expect(currentServiceWorkerRegistration()).resolves.toBeUndefined();
  expect(listenToServiceWorker(() => undefined)()).toBeUndefined();
});

test("a worker that is never ready times out as worker_not_ready; a throwing one fails the same way", async () => {
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
  vi.useFakeTimers();
  vi.stubGlobal("navigator", { serviceWorker: { ready: new Promise(() => undefined) } });
  const pending = expect(readyServiceWorkerRegistration(8000)).rejects.toThrow("worker_not_ready");
  await vi.advanceTimersByTimeAsync(8000);
  await pending;
  vi.stubGlobal("navigator", { serviceWorker: { ready: Promise.reject(new DOMException("x", "InvalidStateError")) } });
  await expect(readyServiceWorkerRegistration(8000)).rejects.toThrow("worker_not_ready");
});

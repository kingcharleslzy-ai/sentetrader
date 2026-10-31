// Protects the startup invariant: a browser profile whose service-worker storage is broken
// (InvalidStateError on every call, or a promise that never settles) still renders the app.
import { afterEach, expect, test, vi } from "vitest";

const render = vi.fn();
vi.mock("react-dom/client", () => ({ default: { createRoot: () => ({ render }) } }));
vi.mock("./App", () => ({ App: () => null }));

function stubWorker(worker: unknown) {
  Object.defineProperty(navigator, "serviceWorker", { configurable: true, value: worker });
}

afterEach(() => {
  Reflect.deleteProperty(navigator, "serviceWorker");
  vi.useRealTimers();
  vi.resetModules();
  render.mockClear();
  vi.restoreAllMocks();
});

test("the app renders when reading service worker registrations throws InvalidStateError", async () => {
  const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  stubWorker({
    getRegistrations: () => Promise.reject(new DOMException("broken storage", "InvalidStateError")),
    register: () => Promise.reject(new DOMException("broken storage", "InvalidStateError")),
  });
  await import("./main");
  await vi.waitFor(() => expect(render).toHaveBeenCalledOnce());
  expect(warn).toHaveBeenCalled();
});

test("the app renders when the browser refuses to hand out the service worker container", async () => {
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
  Object.defineProperty(navigator, "serviceWorker", { configurable: true,
    get() { throw new DOMException("denied", "SecurityError"); } });
  await import("./main");
  await vi.waitFor(() => expect(render).toHaveBeenCalledOnce());
});

test("the app renders when a service worker call never settles", async () => {
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
  stubWorker({ getRegistrations: () => new Promise(() => undefined), ready: new Promise(() => undefined) });
  vi.useFakeTimers();
  await import("./main");
  expect(render).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(3000);
  expect(render).toHaveBeenCalledOnce();
});

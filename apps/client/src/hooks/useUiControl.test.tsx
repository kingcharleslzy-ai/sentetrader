import { renderHook } from "@testing-library/react";
import { requestJson } from "../api";
import { agentConnectionInfo, captureNativeWindow } from "../nativeAgent";
import { useUiControl, useUiControlHealth, type UiState } from "./useUiControl";
import { logDesktop } from "../frontendLog";
import * as runtimeFeed from "../runtimeFeed";

vi.mock("../api", () => ({ requestJson: vi.fn() }));
vi.mock("../frontendLog", () => ({ logDesktop: vi.fn() }));
vi.mock("../nativeAgent", () => ({
  agentConnectionInfo: vi.fn(async () => ({ deviceId: "d", platform: "macos", version: "test", nativeCapture: true })),
  captureNativeWindow: vi.fn(),
  registerAgentWindow: vi.fn(),
  forgetAgentWindow: vi.fn(),
}));
vi.mock("../uiObservation", () => ({
  createUiObserver: () => ({ read: () => ({ capturedAt: "now", viewport: { width: 1, height: 1 }, text: "", truncated: false, regions: [], controls: [], context: {} }), close: () => undefined }),
}));

const state = { symbol: "SHFE.rb2610", duration: 60, view: "trade" } as unknown as UiState;

beforeEach(() => { vi.useFakeTimers(); vi.mocked(requestJson).mockReset(); vi.mocked(captureNativeWindow).mockReset(); });
afterEach(() => { vi.useRealTimers(); });

test.each(["f59f39ecf7aabbf111600f2cd669049f1facab7e", null, undefined])(
  "the window heartbeat reports its build without MCP configuration (%s)", async build => {
    vi.mocked(agentConnectionInfo).mockResolvedValueOnce({ deviceId: "local-device", platform: "macos",
      version: "test", build, nativeCapture: true, mcp: { command: "runtime", args: ["mcp"] } });
    vi.mocked(requestJson).mockResolvedValue({ command: null });
    const view = renderHook(() => useUiControl("http://runtime.test", "owner", state, () => undefined, {}));
    await vi.advanceTimersByTimeAsync(50);
    const device = vi.mocked(requestJson).mock.calls.map(([, init]) => JSON.parse(String(init?.body ?? "{}")))
      .find(body => body.device)?.device;
    expect(device).toBeDefined();
    expect(device.build).toBe(build);
    expect(device).not.toHaveProperty("mcp");
    if (build === undefined) expect(device).not.toHaveProperty("build");
    view.unmount();
  },
);

test("the heartbeat keeps beating while a native capture is stuck", async () => {
  let pulses = 0;
  vi.mocked(requestJson).mockImplementation(async (_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}"));
    if (body.closing) return {};
    pulses += 1;
    // The first pulse dispatches a capture; the client must not wait for it.
    return pulses === 1 ? { command: null, capture: { requestId: "cap", expectedRevision: 0 } } : { command: null };
  });
  vi.mocked(captureNativeWindow).mockImplementation(() => new Promise(() => undefined)); // never resolves
  const view = renderHook(() => useUiControl("http://runtime.test", "owner", state, () => undefined, {}));
  await vi.advanceTimersByTimeAsync(50);
  expect(pulses).toBe(1);
  await vi.advanceTimersByTimeAsync(3000);
  expect(pulses).toBeGreaterThanOrEqual(5);
  expect(captureNativeWindow).toHaveBeenCalledTimes(1);
  view.unmount();
});

test("a finished capture receipt rides on the next heartbeat", async () => {
  const bodies: Array<Record<string, unknown>> = [];
  vi.mocked(requestJson).mockImplementation(async (_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}"));
    if (body.closing) return {};
    bodies.push(body);
    return bodies.length === 1 ? { command: null, capture: { requestId: "cap", expectedRevision: 0 } } : { command: null };
  });
  vi.mocked(captureNativeWindow).mockResolvedValue({ requestId: "cap", observedRevision: 0, capturedAt: "now", image: { mimeType: "image/png", data: "px" } });
  const view = renderHook(() => useUiControl("http://runtime.test", "owner", state, () => undefined, {}));
  await vi.advanceTimersByTimeAsync(1200);
  expect(bodies.some(body => (body.captureReceipt as { requestId?: string } | undefined)?.requestId === "cap")).toBe(true);
  view.unmount();
});

test("panel: positions is receipted as applied once the window reports the 持仓面板", async () => {
  const receipts: unknown[] = [];
  let pulses = 0;
  vi.mocked(requestJson).mockImplementation(async (_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}"));
    if (body.closing) return {};
    pulses += 1;
    if (body.receipt) receipts.push(body.receipt);
    return pulses === 1 ? { command: { commandId: "c1", expectedRevision: 0, changes: { panel: "positions" } } } : { command: null };
  });
  const applied: unknown[] = [];
  const barState = { ...state, panel: "positions", positionsBar: "collapsed" } as unknown as UiState;
  const view = renderHook(() => useUiControl("http://runtime.test", "owner", barState, changes => { applied.push(changes); }, {}));
  await vi.advanceTimersByTimeAsync(50);
  expect(applied).toEqual([{ panel: "positions" }]);
  await vi.advanceTimersByTimeAsync(100);
  expect(receipts).toEqual([{ commandId: "c1", status: "applied" }]);
  view.unmount();
});

test("a state change pulses after it settles instead of waiting for the idle heartbeat", async () => {
  const revisions: number[] = [];
  vi.mocked(requestJson).mockImplementation(async (_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}"));
    if (body.closing) return {};
    revisions.push(body.revision);
    return { command: null };
  });
  const view = renderHook(({ current }) => useUiControl("http://runtime.test", "owner", current, () => undefined, {}),
    { initialProps: { current: { ...state, positionsBar: "expanded" } as unknown as UiState } });
  await vi.advanceTimersByTimeAsync(50);
  expect(revisions).toEqual([0]);
  view.rerender({ current: { ...state, positionsBar: "collapsed" } as unknown as UiState });
  await vi.advanceTimersByTimeAsync(300);
  expect(revisions).toEqual([0, 1]);
  view.unmount();
});

test("a drag's run of state changes collapses into one pulse", async () => {
  // While the feed is live the idle heartbeat is 10 s, so the only pulses inside the window are the ones we cause.
  const live = vi.spyOn(runtimeFeed, "feedLiveNow").mockReturnValue(true);
  const revisions: number[] = [];
  vi.mocked(requestJson).mockImplementation(async (_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}"));
    if (body.closing) return {};
    revisions.push(body.revision);
    return { command: null };
  });
  const view = renderHook(({ current }) => useUiControl("http://runtime.test", "owner", current, () => undefined, {}),
    { initialProps: { current: { ...state, dockWidth: 340 } as unknown as UiState } });
  await vi.advanceTimersByTimeAsync(50);
  for (const dockWidth of [341, 342, 343]) {
    view.rerender({ current: { ...state, dockWidth } as unknown as UiState });
    await vi.advanceTimersByTimeAsync(100);
  }
  await vi.advanceTimersByTimeAsync(300);
  expect(revisions).toEqual([0, 3]);
  view.unmount();
  live.mockRestore();
});

test("three consecutive 422 heartbeats surface as an unregistered window (and recover on the next 200)", async () => {
  let pulses = 0;
  vi.mocked(requestJson).mockImplementation(async (_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}"));
    if (body.closing) return {};
    pulses += 1;
    if (pulses <= 3) throw Object.assign(new Error("422 ..."), { status: 422, detail: { detail: [{ loc: ["body", "state", "chartAnnotations", 0, "label"], msg: "String should have at most 8 characters" }] } });
    return { command: null };
  });
  const view = renderHook(() => ({ control: useUiControl("http://runtime.test", "owner", state, () => undefined, {}), health: useUiControlHealth() }));
  await vi.advanceTimersByTimeAsync(50);
  expect(view.result.current.health.registered).toBe(true);
  await vi.advanceTimersByTimeAsync(1200);
  expect(pulses).toBeGreaterThanOrEqual(3);
  expect(view.result.current.health.registered).toBe(false);
  expect(view.result.current.health.detail).toContain("chartAnnotations");
  expect(logDesktop).toHaveBeenCalledWith("ui-pulse", expect.stringContaining("422"), expect.stringContaining("chartAnnotations"));
  await vi.advanceTimersByTimeAsync(1500);
  expect(view.result.current.health.registered).toBe(true);
  view.unmount();
});

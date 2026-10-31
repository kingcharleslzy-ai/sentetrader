import { invoke } from "@tauri-apps/api/core";
import { CAPTURE_NATIVE_DEADLINE_MS, captureNativeWindow } from "./nativeAgent";
import { observeUi } from "./uiObservation";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("./uiObservation", () => ({ observeUi: vi.fn() }));

beforeEach(() => {
  document.body.innerHTML = "";
  vi.mocked(invoke).mockReset();
  vi.mocked(observeUi).mockReturnValue({ capturedAt: "now", viewport: { width: 1280, height: 720 },
    text: "交易台", truncated: false, regions: [], controls: [], context: { inputValuesOmitted: true } });
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { callback(0); return 1; });
});
afterEach(() => vi.unstubAllGlobals());

test("native capture hides editable contents and removes masking after failure", async () => {
  document.body.innerHTML = '<input value="private"><textarea>private</textarea><div contenteditable="plaintext-only">private</div>';
  vi.mocked(invoke).mockImplementation(async () => {
    for (const element of document.body.children) expect(getComputedStyle(element).visibility).toBe("hidden");
    throw new Error("native failure");
  });
  const result = await captureNativeWindow({ requestId: crypto.randomUUID(), expectedRevision: 0 }, () => 0, () => ({}));
  expect(result.error).toBe("capture_unavailable");
  expect(getComputedStyle(document.querySelector("input")!).visibility).not.toBe("hidden");
});

test("page changes during capture discard image content", async () => {
  let revision = 0;
  vi.mocked(invoke).mockImplementation(async () => { revision = 1; return { mimeType: "image/png", data: "pixels" }; });
  const result = await captureNativeWindow({ requestId: crypto.randomUUID(), expectedRevision: 0 }, () => revision, () => ({}));
  expect(result.error).toBe("page_changed");
  expect(result.detail).toBe("revision");
  expect(result.image).toBeUndefined();
});

const observation = (overrides: Partial<ReturnType<typeof observeUi>>) => ({ capturedAt: "now", viewport: { width: 1280, height: 720 },
  text: "交易台", truncated: false, regions: [], controls: [], context: { inputValuesOmitted: true, market: { capturedAt: "t0" } }, ...overrides });

test("a feed refresh of the observation context during capture does not reject the image", async () => {
  // The Runtime feed re-publishes the market snapshot every 5 s with a new capturedAt; the picture is unchanged.
  vi.mocked(observeUi).mockReturnValueOnce(observation({}));
  vi.mocked(invoke).mockImplementation(async () => {
    vi.mocked(observeUi).mockReturnValue(observation({ context: { inputValuesOmitted: true, market: { capturedAt: "t1", lastPrice: 3050 } } }));
    return { mimeType: "image/png", data: "pixels" };
  });
  const result = await captureNativeWindow({ requestId: crypto.randomUUID(), expectedRevision: 0 }, () => 0, () => ({}));
  expect(result.error).toBeUndefined();
  expect(result.image).toEqual({ mimeType: "image/png", data: "pixels" });
});

test("visible content changing during capture is rejected and names the changed parts", async () => {
  vi.mocked(observeUi).mockReturnValueOnce(observation({}));
  vi.mocked(invoke).mockImplementation(async () => {
    vi.mocked(observeUi).mockReturnValue(observation({ text: "交易台 · 3,051", controls: [{ label: "确认" }] }));
    return { mimeType: "image/png", data: "pixels" };
  });
  const result = await captureNativeWindow({ requestId: crypto.randomUUID(), expectedRevision: 0 }, () => 0, () => ({}));
  expect(result.error).toBe("page_changed");
  expect(result.detail).toBe("text,controls");
  expect(result.image).toBeUndefined();
});

test("credential dialog rejects capture before invoking native code", async () => {
  document.body.innerHTML = '<section data-agent-private>credentials</section>';
  vi.spyOn(document.querySelector("section")!, "getClientRects").mockReturnValue({ length: 1 } as DOMRectList);
  const result = await captureNativeWindow({ requestId: crypto.randomUUID(), expectedRevision: 0 }, () => 0, () => ({}));
  expect(result.error).toBe("private_window");
  expect(invoke).not.toHaveBeenCalled();
});

test("a webview that stops painting reports the capture unavailable instead of hanging", async () => {
  vi.useFakeTimers();
  try {
    vi.stubGlobal("requestAnimationFrame", () => 1); // occluded window: frames never arrive
    const pending = captureNativeWindow({ requestId: crypto.randomUUID(), expectedRevision: 0 }, () => 0, () => ({}));
    await vi.advanceTimersByTimeAsync(2000);
    const result = await pending;
    expect(result.error).toBe("capture_unavailable");
    expect(invoke).not.toHaveBeenCalled();
    expect(getComputedStyle(document.body).visibility).not.toBe("hidden");
  } finally {
    vi.useRealTimers();
  }
});

test("a native capture that never returns is abandoned after its deadline", async () => {
  vi.useFakeTimers();
  try {
    // Within the Runtime's 10 s expiry even after two paint frames: the native wait is capped at 5 s.
    expect(CAPTURE_NATIVE_DEADLINE_MS).toBe(5000);
    vi.mocked(invoke).mockImplementation(() => new Promise(() => undefined));
    const pending = captureNativeWindow({ requestId: crypto.randomUUID(), expectedRevision: 0 }, () => 0, () => ({}));
    await vi.advanceTimersByTimeAsync(5100);
    const result = await pending;
    expect(result.error).toBe("capture_unavailable");
    expect(document.head.querySelector("style")).toBeNull();
  } finally {
    vi.useRealTimers();
  }
});
